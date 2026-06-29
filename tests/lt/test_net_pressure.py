"""
Component tests for contracts/net_pressure/YBNetPressure.vy.

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
import math
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
    return boa.load('contracts/net_pressure/YBNetPressure.vy')


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


def _pool_metrics_py(cryptopool, lp_oracle_2):
    """Reproduce YBNetPressure._pool_metrics in Python: (x_frac, lp_oracle, lp_ps)."""
    price_oracle = cryptopool.price_oracle()
    price_scale = cryptopool.price_scale()
    vprice = cryptopool.virtual_price()
    D = cryptopool.D()
    supply = cryptopool.totalSupply()
    A_raw = cryptopool.A() // 2
    p = price_oracle * 10**18 // price_scale
    x, y = lp_oracle_2.internal._get_x_y(A_raw, p)
    pv = x + p * y // 10**18
    x_frac = x * 10**18 // pv
    lp_price_oracle = pv * D // supply
    lp_price_ps = 2 * vprice * math.isqrt(price_scale * 10**18) // 10**18
    return x_frac, lp_price_oracle, lp_price_ps


def _ratio(cryptopool):
    return cryptopool.price_oracle() / cryptopool.price_scale()


def _arb_amm_to_price(yb_amm, cryptopool, ct, sc, acct, target):
    """Arbitrage the YB AMM's marginal price get_p() to `target` (crvUSD/LP) with
    closed-form constant-product steps. Returns the reached get_p()."""
    with boa.env.prank(acct):
        cryptopool.approve(yb_amm.address, 2**256 - 1)
        sc.approve(yb_amm.address, 2**256 - 1)
    for _ in range(60):
        st = yb_amm.get_state()
        x_initial = st.x0 - st.debt
        gp = x_initial * 10**18 // st.collateral
        if abs(gp - target) * 500 < target:        # within 0.2%
            break
        k = x_initial * st.collateral               # constant product (x0 ~ const)
        coll_t = math.isqrt(k * 10**18 // target)
        if coll_t > st.collateral:                  # lower get_p: sell LP into AMM
            sell = min(coll_t - st.collateral, st.collateral // 4 + 1)  # cap step
            b0, b1 = cryptopool.balances(0), cryptopool.balances(1)
            supply = cryptopool.totalSupply()
            need0 = b0 * sell // supply * 3 + 10**18  # balanced add -> no price move
            need1 = b1 * sell // supply * 3 + 10**18
            sc._mint_for_testing(acct, need0)
            ct._mint_for_testing(acct, need1)
            with boa.env.prank(acct):
                cryptopool.add_liquidity([need0, need1], 0)
                lp = cryptopool.balanceOf(acct)
                try:
                    yb_amm.exchange(1, 0, min(sell, lp), 0)
                except Exception:
                    break
        else:                                       # raise get_p: buy LP from AMM
            buy = min(st.debt - math.isqrt(k * target // 10**18), st.debt // 4 + 1)
            sc._mint_for_testing(acct, buy + 10**18)
            with boa.env.prank(acct):
                try:
                    yb_amm.exchange(0, 1, max(buy, 1), 0)
                except Exception:
                    break
    st = yb_amm.get_state()
    return (st.x0 - st.debt) * 10**18 // st.collateral


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
def test_oracle_fallback_when_amm_non_tradable(
    cryptopool, yb_lt, yb_amm, cryptopool_oracle, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, net_pressure, mock_agg,
    lp_oracle_2, extra_depth, deposit,
):
    """
    When get_state()/get_x0 revert (AMM non-tradable) the oracle backs off to the
    AMM's raw collateral/debt but keeps the price_oracle crvUSD split (the Curve
    pool never reverts). We force the revert by crashing agg so coll_value falls
    below the get_x0 solvency bound.
    """
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    yb_amm.get_state()  # works before the crash
    with boa.env.prank(admin):
        mock_agg.set_price(10**17)  # coll_value << 16/9 * debt -> get_x0 reverts
    with pytest.raises(Exception):
        yb_amm.get_state()

    # Oracle must still price (fallback branch), not revert.
    net = net_pressure.net_pressure_oracle(yb_lt.address)

    # Manual fallback: raw AMM amounts, oracle-based split.
    debt = yb_amm.get_debt()
    collateral = yb_amm.collateral_amount()
    p_o_amm = cryptopool_oracle.price()
    x_frac, lp_price_oracle, lp_price_ps = _pool_metrics_py(cryptopool, lp_oracle_2)
    coll_value_ps = collateral * p_o_amm // 10**18
    calc_coll_value = coll_value_ps * lp_price_oracle // lp_price_ps
    expected = debt - calc_coll_value * x_frac // 10**18
    assert net == expected

    # The crvUSD split is still non-manipulable: a pool swap barely moves it,
    # since collateral/debt are unchanged and the split rides on price_oracle.
    attacker = accounts[1]
    amt = (extra_depth + 1) * 3 * 10**18
    collateral_token._mint_for_testing(attacker, amt)
    with boa.env.prank(attacker):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.exchange(1, 0, amt, 0)
    net1 = net_pressure.net_pressure_oracle(yb_lt.address)
    assert abs(net1 - net) < debt // 50


@pytest.mark.parametrize("extra_depth,deposit", [(50, 3 * 10**18), (10, 10**18)])
def test_oracle_matches_naive_with_imbalanced_pool(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, net_pressure, lp_oracle_2, extra_depth, deposit,
):
    """
    With agg == 1, oracle ~= naive should hold even when the Curve pool is
    imbalanced (r != 1), *provided* (a) the YB AMM is arbitraged to the real LP
    price and (b) price_oracle has had time to converge to the frozen spot
    composition (so naive's spot split matches the oracle's price_oracle split).
    The bonding-curve slide then cancels and only the AMM's residual imbalance
    and rounding remain.
    """
    _setup(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin,
           extra_depth, deposit)

    # Push the pool price down a bit (a few small swaps), then WAIT long so the
    # EMA price_oracle converges to last_prices; price_scale needs trades to
    # repeg, so it stays put and r = price_oracle/price_scale != 1.
    dumper = accounts[3]
    with boa.env.prank(dumper):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
    for _ in range(5):
        amt = cryptopool.balances(1) // 12
        collateral_token._mint_for_testing(dumper, amt)
        with boa.env.prank(dumper):
            try:
                cryptopool.exchange(1, 0, amt, 0)
            except Exception:
                break
        boa.env.time_travel(600)
    for _ in range(40):                       # wait: price_oracle -> last_prices
        boa.env.time_travel(1200)

    r = _ratio(cryptopool)
    assert abs(r - 1.0) > 0.02, f"pool not imbalanced enough: r={r}"

    # Arbitrage the YB AMM to the real LP price (else it sits stale at lp_price_ps).
    _, lp_price_oracle, _ = _pool_metrics_py(cryptopool, lp_oracle_2)
    gp = _arb_amm_to_price(yb_amm, cryptopool, collateral_token, stablecoin,
                           accounts[1], lp_price_oracle)

    equity = yb_amm.get_state().x0 // 3
    oracle = net_pressure.net_pressure_oracle(yb_lt.address)
    naive = net_pressure.net_pressure_naive(yb_lt.address)
    print(f"\n[depth={extra_depth}] r={r:.3f} get_p/target={gp/lp_price_oracle:.4f} "
          f"equity={equity/1e18:.0f} oracle={oracle/1e18:.2f} naive={naive/1e18:.2f} "
          f"diff/equity={abs(oracle-naive)/equity:.4f}")
    assert abs(oracle - naive) < equity // 20   # within ~5% of equity despite r != 1


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
    half0 = net_pressure.half_tvl_oracle(yb_lt.address)
    coll0 = yb_amm.collateral_amount()  # spot LP holdings (manipulable)

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
    half1 = net_pressure.half_tvl_oracle(yb_lt.address)
    coll1 = yb_amm.collateral_amount()
    print(f"\n[depth={extra_depth} dep={deposit}] equity={equity/1e18:.0f} "
          f"oracle {oracle0/1e18:.4f} -> {oracle1/1e18:.4f}")
    # x0 only grows by fees on an AMM trade, so the oracle is essentially unchanged.
    assert abs(oracle1 - oracle0) < equity // 50
    # The trade moved the spot collateral, but half-TVL (x0-based) is unmoved -
    # i.e. half_tvl_oracle is NOT the manipulable lp_price * collateral_amount.
    assert abs(coll1 - coll0) > coll0 // 100, "trade should have moved spot collateral"
    assert abs(half1 - half0) < equity // 50
