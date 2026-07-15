"""
The YB oracles (YBNetPressure, YBLendingOracle, YBLendingOracleLL) reproduce
AMM.get_x0 in-contract instead of calling AMM.get_state(). get_state() re-enters
the crvUSD aggregator, so a gas-metered raw_call around it (the old
`assert msg.gas > gas_before // 16` guard) could not robustly separate a genuine
"too imbalanced for x0" revert from a forced out-of-gas.

This test pins the replication to the AMM itself: the in-contract x0 must equal
AMM.get_state().x0 bit-for-bit, and the arithmetic solvability test must agree
with whether get_state() reverts, across a spread of pool/AMM states and right
across the get_x0 solvency boundary. If the AMM's leverage math or LEV_RATIO ever
drifts from the oracle copy, this fails.
"""
import math
import boa

# Mirror AMM.__init__ exactly (leverage == L * 1e18 == 2e18 system-wide).
LEVERAGE = 2 * 10**18
DENOM = 2 * LEVERAGE - 10**18
LEV_RATIO = LEVERAGE**2 * 10**18 // DENOM**2


def _x0_of(state):
    try:
        return state.x0
    except AttributeError:
        return state[2]


def _solve_x0(p_o, collateral, debt):
    """Python replica of AMM.get_x0(p_o, collateral, debt, safe_limits=False).
    COLLATERAL_PRECISION == 1 (cryptopool LP is 18-dec). Returns (x0, solvable)."""
    coll_value = p_o * collateral // 10**18
    d_sub = 4 * coll_value * LEV_RATIO // 10**18 * debt
    if coll_value * coll_value < d_sub:
        return 0, False
    x0 = (coll_value + math.isqrt(coll_value * coll_value - d_sub)) * 10**18 // (2 * LEV_RATIO)
    return x0, True


def _open(cryptopool, yb_lt, collateral_token, admin, deposit):
    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, deposit)
    with boa.env.prank(admin):
        yb_lt.deposit(deposit, p * deposit // 10**18, 0)
        yb_lt.set_rate(0)  # freeze interest so debt is stationary across reads
    for _ in range(10):
        boa.env.time_travel(1200)


def _check(yb_amm, oracle):
    """The oracle's p_o and the in-contract get_x0 must match the AMM exactly."""
    p_o = oracle.price()
    collateral = yb_amm.collateral_amount()
    debt = yb_amm.get_debt()
    x0_mine, solvable = _solve_x0(p_o, collateral, debt)
    try:
        got = _x0_of(yb_amm.get_state())
        reverted = False
    except Exception:
        reverted = True
    assert solvable == (not reverted), (solvable, reverted, p_o, collateral, debt)
    if solvable:
        assert x0_mine == got, (x0_mine, got)


def test_x0_replication_matches_get_state(
        cryptopool, yb_amm, yb_lt, cryptopool_oracle, collateral_token, stablecoin,
        accounts, admin, mock_agg, yb_allocated, seed_cryptopool):
    _open(cryptopool, yb_lt, collateral_token, admin, 10**18)

    # (a) equilibrium, solvable
    _check(yb_amm, cryptopool_oracle)

    # (b) p_o via lp_price_ps * agg_price reproduces PRICE_ORACLE_CONTRACT.price()
    #     exactly - the identity the in-contract x0 relies on.
    vprice = cryptopool.virtual_price()
    price_scale = cryptopool.price_scale()
    lp_price_ps = 2 * vprice * math.isqrt(price_scale * 10**18) // 10**18
    assert yb_lt.agg() == mock_agg.address
    assert lp_price_ps * mock_agg.price() // 10**18 == cryptopool_oracle.price()

    # (c) move the pool with swaps, re-check exactness at several oracle prices
    for who, dx in [(accounts[2], 3), (accounts[2], 12), (accounts[3], 40)]:
        stablecoin._mint_for_testing(who, dx * 100_000 * 10**18)
        collateral_token._mint_for_testing(who, dx * 10**18)
        with boa.env.prank(who):
            stablecoin.approve(cryptopool.address, 2**256 - 1)
            collateral_token.approve(cryptopool.address, 2**256 - 1)
            cryptopool.exchange(1, 0, dx * 10**17, 0)
        for _ in range(5):
            boa.env.time_travel(1200)
        _check(yb_amm, cryptopool_oracle)

    # (d) sweep agg price down toward and across the get_x0 solvency boundary,
    #     where get_state() flips from success to revert.
    for ap in [2 * 10**18, 10**18, 6 * 10**17, 4 * 10**17, 3 * 10**17, 2 * 10**17, 10**17]:
        with boa.env.prank(admin):
            mock_agg.set_price(ap)
        _check(yb_amm, cryptopool_oracle)


def _amm_get_x0(yb_amm, p_o, collateral, debt):
    """AMM.get_x0(p_o, collateral, debt, safe_limits=False) -> (x0, solvable)."""
    try:
        return yb_amm.internal.get_x0(p_o, collateral, debt, False), True
    except Exception:
        return 0, False


def test_get_x0_internal_matches_amm(
        yb_amm, lending_oracle, ll_deployer, yb_lt):
    """
    Each oracle exposes the same reproduced get_x0 as an internal `_get_x0(p_oracle,
    collateral, debt) -> (solvable, x0)`. Pin all three, bit-for-bit, to the AMM's own
    `get_x0(..., safe_limits=False)` across a grid that straddles the solvency boundary
    (debt == 9/16 * coll_value at L=2): same x0 where the AMM succeeds, and `solvable`
    False exactly where the AMM reverts.
    """
    # The reproduction relies on COLLATERAL_PRECISION == 1 (the cryptopool LP collateral is
    # 18-dec, so the AMM's coll_value scaling is a no-op). That identity isn't asserted
    # directly - COLLATERAL_PRECISION is a non-public immutable - but the equivalence below
    # proves it: if it were != 1 the AMM's get_x0 would diverge from the oracle copy.
    oracles = [
        ("YBLendingOracle", lending_oracle),
        ("YBLendingOracleLL", ll_deployer.deploy()),   # _get_x0 is @pure; no initialize needed
        ("YBNetPressure", boa.load('contracts/net_pressure/YBNetPressure.vy')),
    ]

    checked = solvable_seen = revert_seen = 0
    for p_o in [5 * 10**17, 10**18, 2 * 10**18, 3 * 10**18, 12345 * 10**14]:
        for collateral in [0, 7 * 10**17, 10**18, 3 * 10**18, 55 * 10**17]:
            cv = p_o * collateral // 10**18
            # Straddle debt == 9/16 * coll_value (the get_x0 revert boundary at L=2).
            edge = cv * 9 // 16
            for debt in sorted(set([0, cv // 4, cv // 2, max(edge - 3, 0), edge,
                                    edge + 3, cv * 3 // 4, cv, cv + 10**18])):
                amm_x0, amm_ok = _amm_get_x0(yb_amm, p_o, collateral, debt)
                for name, oracle in oracles:
                    ok, x0 = oracle.internal._get_x0(p_o, collateral, debt)
                    assert ok == amm_ok, (name, p_o, collateral, debt, ok, amm_ok)
                    if amm_ok:
                        assert x0 == amm_x0, (name, p_o, collateral, debt, x0, amm_x0)
                checked += 1
                solvable_seen += amm_ok
                revert_seen += not amm_ok

    # The grid must actually exercise both sides of the boundary, or it proves nothing.
    assert solvable_seen > 0 and revert_seen > 0, (solvable_seen, revert_seen, checked)
