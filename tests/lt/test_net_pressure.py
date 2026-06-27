"""
Component tests for contracts/utils/YBNetPressure.vy.

Net pressure of a YB market = AMM debt - crvUSD sitting inside the Curve LP tokens
the AMM holds. Positive => crvUSD must be bought on unwind (buy pressure).

Structurally, a 2x-leveraged 50/50 LP has debt == crvUSD-in-LP, so at the
unmanipulated equilibrium (price_oracle == price_scale) net pressure ~= 0. The
signal is the deviation, which the *oracle* flavour must report robustly while the
*naive* flavour can be moved by a single swap.

We test each piece separately, at several pool/AMM balances:
  1. crvusd_value_fraction  - matches lp_oracle_2 wiring AND the spot pool value
     split at equilibrium.
  2. net_pressure_naive     - equals the manual spot subtraction, and IS movable
     by a cryptopool swap.
  3. net_pressure_oracle    - matches the honest ground truth at equilibrium, and
     is NOT materially movable by a cryptopool swap or an AMM trade (it is built
     from the conserved invariant x0 and the EMA price_oracle).
"""
import boa
import pytest


# (extra cryptopool depth as multiples of the base seed, LT deposit in collateral)
BALANCE_CASES = [
    (0, 10**18),        # base seed only, 1.0 collateral deposited
    (10, 10**18),       # deeper pool
    (10, 5 * 10**17),   # deeper pool, smaller position
    (50, 3 * 10**18),   # deep pool, bigger position
]


@pytest.fixture(scope="session")
def net_pressure():
    # Stateless (takes the LT as a call arg); safe to share read-only across tests.
    return boa.load('contracts/utils/YBNetPressure.vy')


def _settle(cryptopool):
    """Let price_oracle (EMA) settle onto price_scale: time-travel, no trades."""
    for _ in range(10):
        boa.env.time_travel(1200)


def _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit):
    """Seed extra pool depth, open an LT position, then settle the EMA.
    Returns nothing - the fixtures (yb_amm etc.) read live state."""
    if extra_depth > 0:
        whale = accounts[2]
        stablecoin._mint_for_testing(whale, extra_depth * 100_000 * 10**18)
        collateral_token._mint_for_testing(whale, extra_depth * 10**18)
        with boa.env.prank(whale):
            stablecoin.approve(cryptopool.address, 2**256 - 1)
            collateral_token.approve(cryptopool.address, 2**256 - 1)
            cryptopool.add_liquidity(
                [extra_depth * 100_000 * 10**18, extra_depth * 10**18], 0)

    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, deposit)
    with boa.env.prank(admin):
        yb_lt.deposit(deposit, p * deposit // 10**18, 0)
        yb_lt.set_rate(0)  # freeze interest so debt is stationary across reads

    _settle(cryptopool)


def _ratio(cryptopool):
    return cryptopool.price_oracle() / cryptopool.price_scale()


def _open_ema_gap(cryptopool, token, acct, target=0.05):
    """Drift price_oracle (EMA) away from price_scale with small swaps + time
    travel, so r = price_oracle/price_scale != 1. Returns the reached ratio."""
    with boa.env.prank(acct):
        token.approve(cryptopool.address, 2**256 - 1)
    for _ in range(15):
        amt = cryptopool.balances(1) // 15
        if amt == 0:
            break
        token._mint_for_testing(acct, amt)
        with boa.env.prank(acct):
            try:
                cryptopool.exchange(1, 0, amt, 0)  # sell crypto -> push price down
            except Exception:
                break
        for _ in range(4):
            boa.env.time_travel(1200)
        p = cryptopool.price_oracle() / cryptopool.price_scale()
        if abs(p - 1.0) > target:
            break
    return cryptopool.price_oracle() / cryptopool.price_scale()


# ---------------------------------------------------------------------------
# 1. crvusd_value_fraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_crvusd_value_fraction_wiring(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, lp_oracle_2, extra_depth, deposit,
):
    """Contract reproduces lp_oracle_2 exactly (A scaling + price arg + x/(x+p*y))."""
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    price_oracle = cryptopool.price_oracle()
    price_scale = cryptopool.price_scale()
    A_raw = cryptopool.A() // 2  # N=2: A_pool * 1e4 // (2 * 1e4)
    p = price_oracle * 10**18 // price_scale

    x, y = lp_oracle_2.internal._get_x_y(A_raw, p)
    pv = x + p * y // 10**18
    frac_py = x * 10**18 // pv

    frac = net_pressure.crvusd_value_fraction(yb_lt.address)
    assert frac == frac_py


@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_crvusd_value_fraction_economic(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """At equilibrium the oracle crvUSD share matches the pool's spot value split."""
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    assert abs(_ratio(cryptopool) - 1.0) < 0.02, "EMA did not settle"

    price_scale = cryptopool.price_scale()
    bal0 = cryptopool.balances(0)                       # crvUSD
    bal1_value = cryptopool.balances(1) * price_scale // 10**18  # crypto -> crvUSD
    spot_frac = bal0 / (bal0 + bal1_value)

    frac = net_pressure.crvusd_value_fraction(yb_lt.address) / 1e18
    # ~0.5 for a pool sitting at its scale; matches the spot value split.
    assert abs(frac - spot_frac) < 1e-3, f"frac={frac} spot={spot_frac}"


# ---------------------------------------------------------------------------
# 2. net_pressure_naive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_naive_matches_manual(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """naive == debt - collateral * balances[0] / totalSupply, exactly."""
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    debt = yb_amm.get_debt()
    collateral = yb_amm.collateral_amount()
    bal0 = cryptopool.balances(0)
    supply = cryptopool.totalSupply()
    expected = debt - collateral * bal0 // supply

    assert net_pressure.net_pressure_naive(yb_lt.address) == expected


@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_naive_is_manipulable(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """A single in-block cryptopool swap moves the naive value a lot."""
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    naive0 = net_pressure.net_pressure_naive(yb_lt.address)
    equity = yb_amm.get_state().x0 // 3  # crvUSD (agg == 1 in tests)

    # Attacker dumps crypto into the pool, inflating balances[0] (crvUSD reserve).
    attacker = accounts[1]
    amt = (extra_depth + 1) * 5 * 10**18
    collateral_token._mint_for_testing(attacker, amt)
    with boa.env.prank(attacker):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.exchange(1, 0, amt, 0)  # no time travel: same block

    naive1 = net_pressure.net_pressure_naive(yb_lt.address)
    # The manipulation is large relative to the position's equity.
    assert abs(naive1 - naive0) > equity // 20, (
        f"naive barely moved: {naive0} -> {naive1} (equity {equity})")


# ---------------------------------------------------------------------------
# 3. net_pressure_oracle
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_oracle_matches_ground_truth_at_equilibrium(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """
    At the unmanipulated equilibrium the oracle must agree with the honest naive
    value, and both must be ~0 (debt == crvUSD-in-LP for a 2x 50/50 LP).
    Ground truth is the naive value *here*, where there is nothing to manipulate.
    """
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    assert abs(_ratio(cryptopool) - 1.0) < 0.02, "EMA did not settle"

    equity = yb_amm.get_state().x0 // 3
    oracle = net_pressure.net_pressure_oracle(yb_lt.address)
    naive = net_pressure.net_pressure_naive(yb_lt.address)

    # Baseline ~0 and agreement with the honest spot value.
    assert abs(oracle) < equity // 100, f"oracle not ~0: {oracle} (equity {equity})"
    assert abs(oracle - naive) < equity // 100, f"oracle {oracle} vs naive {naive}"


@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_oracle_resists_pool_swap(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """Same swap that swings naive must barely move the oracle."""
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    equity = yb_amm.get_state().x0 // 3
    oracle0 = net_pressure.net_pressure_oracle(yb_lt.address)
    naive0 = net_pressure.net_pressure_naive(yb_lt.address)

    attacker = accounts[1]
    amt = (extra_depth + 1) * 5 * 10**18
    collateral_token._mint_for_testing(attacker, amt)
    with boa.env.prank(attacker):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.exchange(1, 0, amt, 0)

    oracle1 = net_pressure.net_pressure_oracle(yb_lt.address)
    naive1 = net_pressure.net_pressure_naive(yb_lt.address)

    naive_move = abs(naive1 - naive0)
    oracle_move = abs(oracle1 - oracle0)
    print(f"\n[depth={extra_depth} dep={deposit}] equity={equity/1e18:.0f} "
          f"naive_move={naive_move/1e18:.2f} oracle_move={oracle_move/1e18:.4f}")

    assert naive_move > equity // 20, "swap should have moved naive a lot"
    # Oracle shifts only via the small price_scale/vprice repeg, not the spot split.
    assert oracle_move < naive_move // 10
    assert oracle_move < equity // 50


@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_oracle_converges_to_trivial_only_at_equilibrium(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """
    The bonding-curve oracle equals the trivial equilibrium formula
    equity*((L-1) - L*x_frac)  iff  price_oracle == price_scale (r == 1).
    There it is ~0 (x_frac forced to 0.5). Once the pool EMA gap opens (r != 1)
    the sqrt(r) correction kicks in and the two diverge.
    """
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    def trivial():
        equity = yb_amm.get_state().x0 // 3            # x0/(2L-1), agg == 1
        x_frac = net_pressure.crvusd_value_fraction(yb_lt.address)
        return equity * (10**18 - 2 * x_frac) // 10**18  # equity*(1 - 2*x_frac), L=2

    # --- settled: r ~= 1 -> converges to the trivial result, both ~0 ---
    assert abs(_ratio(cryptopool) - 1.0) < 0.02
    equity = yb_amm.get_state().x0 // 3
    net0 = net_pressure.net_pressure_oracle(yb_lt.address)
    triv0 = trivial()
    assert abs(net0) < equity // 100, f"settled net not ~0: {net0}"
    assert abs(net0 - triv0) < equity // 100, f"net {net0} != trivial {triv0} at r~1"

    # --- open a pool EMA gap: small swaps then let price_oracle (EMA) drift ---
    p = _open_ema_gap(cryptopool, collateral_token, accounts[3])
    if abs(p - 1.0) <= 0.03:
        pytest.skip(f"pool too thin to open an EMA gap (p={p})")

    net1 = net_pressure.net_pressure_oracle(yb_lt.address)
    triv1 = trivial()
    # At r != 1 the sqrt(r) correction makes the two materially disagree.
    assert abs(net1 - triv1) > equity // 50, (
        f"expected divergence at p={p}: net={net1} trivial={triv1}")


@pytest.mark.parametrize("extra_depth,deposit", BALANCE_CASES)
def test_oracle_resists_amm_trade(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, extra_depth, deposit,
):
    """
    Trading against the LevAMM changes actual collateral & debt (which naive uses)
    but conserves x0 (which the oracle uses), so the oracle stays put.
    """
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    equity = yb_amm.get_state().x0 // 3
    oracle0 = net_pressure.net_pressure_oracle(yb_lt.address)

    attacker = accounts[1]
    # Buy some LP collateral out of the AMM with stablecoin.
    debt = yb_amm.get_debt()
    stable_in = debt // 4
    stablecoin._mint_for_testing(attacker, stable_in)
    with boa.env.prank(attacker):
        stablecoin.approve(yb_amm.address, 2**256 - 1)
        try:
            yb_amm.exchange(0, 1, stable_in, 0)
        except Exception as e:
            if any(x in str(e) for x in ("Unsafe", "Bad final state", "Amount too large")):
                pytest.skip("AMM rejected the probe trade in this state")
            raise

    oracle1 = net_pressure.net_pressure_oracle(yb_lt.address)
    print(f"\n[depth={extra_depth} dep={deposit}] equity={equity/1e18:.0f} "
          f"oracle {oracle0/1e18:.4f} -> {oracle1/1e18:.4f}")
    # x0 only grows by fees on an AMM trade, so the oracle is essentially unchanged.
    assert abs(oracle1 - oracle0) < equity // 50
