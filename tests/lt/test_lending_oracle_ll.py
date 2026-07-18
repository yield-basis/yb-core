"""
YBLendingOracleLL: ybLT oracle with an honest EMA on the FUNDAMENTAL rate.

The price decomposes as fundamental * shift: the fundamental is the LT->asset conversion rate
at price == price_scale (a la pricePerShare) and absorbs every manipulable level input - AMM.vy
wash-trade fees (via x0) AND pool virtual_price; the shift to price_oracle is a pure
(A, price ratio) function applied live. The LL applies a cryptopool-style EMA to the
fundamental, blending the PREVIOUS checkpoint's value (never the current-block one), so it is
flash-proof against both wash paths. Checks:
  - unseeded / settled LL equals YBLendingOracle (the untouched reference) to rounding,
  - the fundamental EMA lags a fee bump and converges over ~ema_time,
  - same-block wash against the POOL and against the AMM do NOT move the LL price,
  - USD vs asset denomination, factory-settable ema_time.
"""
import os
from types import SimpleNamespace

import boa
import pytest
from hypothesis import given, settings, strategies as st, HealthCheck

# Equivalence-fuzz examples: a real fuzz by default, crankable via env for a heavy campaign
# (e.g. LL_FUZZ_EXAMPLES=1000 uv run pytest ...).
FUZZ_EXAMPLES = int(os.getenv("LL_FUZZ_EXAMPLES", "200"))

EMA_TIME = 866   # half-life = EMA_TIME * ln(2) ~= 600s (10 min); factory default
HALF_LIFE = 600


def _approx(a, b, rel=10**-9):
    assert abs(a - b) <= max(2, int(abs(b) * rel)), f"{a} !~= {b} (rel {abs(a - b) / max(1, abs(b)):.2e})"


def _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin):
    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)
    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, p * 10**18 // 10**18, 0)


def _bump_vprice(cryptopool, collateral_token, stablecoin, accounts):
    """Wash round-trips: fees accrue into the pool -> virtual_price rises. Symmetric, so the
    pool price_oracle barely moves; the increase is essentially all fee (virtual_price)."""
    washer = accounts[3]
    collateral_token._mint_for_testing(washer, 1000 * 10**18)
    stablecoin._mint_for_testing(washer, 1000 * 100_000 * 10**18)
    with boa.env.prank(washer):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        for _ in range(200):
            got = cryptopool.exchange(0, 1, 5000 * 10**18, 0)
            cryptopool.exchange(1, 0, got, 0)


def _deploy_asset_ll(ll_deployer, yb_lt, admin):
    ll = ll_deployer.deploy()
    ll.initialize(yb_lt.address, False, EMA_TIME, admin)   # asset denom; factory=admin
    return ll


def test_ll_equivalence_to_reference(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """Unseeded and freshly-seeded (fundamental_ema == current fundamental), the LL equals
    YBLendingOracle to rounding - the decomposed math must reproduce the reference."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)
    yb = lending_oracle

    ref = yb.price_in_asset(yb_lt.address)
    _approx(ll.price(), ref)               # unseeded: raw fundamental
    _approx(ll.price_w(), ref)             # seed
    assert ll.fundamental_ema() == ll.fundamental_last()  # seeded: ema == last == current
    _approx(ll.price(), ref)               # settled: ema == current fundamental


def test_ll_fundamental_ema_smoothing(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """A fundamental bump (pool-fee wash raises virtual_price -> x0) is folded into the EMA
    only after it survives a checkpoint, then converges over ~ema_time - the honest 'value
    must survive a later block' property."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)

    ll.price_w()                                        # seed: ema = last = f0
    f0 = ll.fundamental_ema()
    assert f0 == ll.fundamental_last()

    _bump_vprice(cryptopool, collateral_token, stablecoin, accounts)

    # Checkpoint #2: advances using the OLD fundamental_last (== f0), so the EMA is unchanged;
    # the bump is only now recorded as fundamental_last for the next advance.
    boa.env.time_travel(EMA_TIME)
    ll.price_w()
    assert ll.fundamental_ema() == f0, "bump entered the EMA before surviving a checkpoint"
    f1 = ll.fundamental_last()                          # the bumped value, now recorded
    assert f1 > f0, "wash did not raise the fundamental"

    # Checkpoint #3 one half-life later: the EMA moves ~halfway from f0 to f1.
    boa.env.time_travel(HALF_LIFE)
    ll.price_w()
    frac = (ll.fundamental_ema() - f0) / (f1 - f0)
    print(f"\nf0={f0} f1={f1} half-life frac={frac:.3f}")
    assert 0.45 < frac < 0.55

    # Many time-constants later: converged to ~f1 (up to slow debt-interest drift).
    boa.env.time_travel(EMA_TIME * 8)
    ll.price_w()
    _approx(ll.fundamental_ema(), f1, rel=10**-3)


def test_ll_flash_resistant(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """A virtual_price pump within a single block moves YBLendingOracle (reads current vprice)
    but NOT the LL, which prices off the committed EMA."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)
    yb = lending_oracle

    ll.price_w()                                        # seed at t0
    p0 = ll.price()
    y0 = yb.price_in_asset(yb_lt.address)

    # Same block (no time travel): pump virtual_price.
    _bump_vprice(cryptopool, collateral_token, stablecoin, accounts)

    p1 = ll.price()
    y1 = yb.price_in_asset(yb_lt.address)
    print(f"\nLL {p0}->{p1} ({(p1-p0)/p0*100:+.4f}%)   ref {y0}->{y1} ({(y1-y0)/y0*100:+.4f}%)")
    assert y1 > y0, "reference did not react to the vprice pump"
    # LL essentially flat: it ignores the same-block fee pump (well within 0.1%).
    assert abs(p1 - p0) <= p0 // 1000
    assert (y1 - y0) > 5 * abs(p1 - p0), "LL should be far less reactive than the reference"


def test_ll_price_w_return_flash_resistant(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """price_w() records the current fundamental as fundamental_last BEFORE it prices, so it
    MUST price off the smoothed (old) EMA it just computed - never the freshly-stored last. A
    same-block pump that precedes price_w() must not move its RETURN value; the pump is only
    recorded for the NEXT advance. Guards the save-before-price ordering in price_w()."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)
    yb = lending_oracle

    seeded = ll.price_w()                                # seed: ema = last = f0
    f0 = ll.fundamental_ema()
    y0 = yb.price_in_asset(yb_lt.address)

    # Same block (no time travel): pump the fundamental (pool-fee wash), THEN checkpoint.
    _bump_vprice(cryptopool, collateral_token, stablecoin, accounts)

    ret = ll.price_w()                                  # stores last = bumped, prices off old EMA
    y1 = yb.price_in_asset(yb_lt.address)

    assert y1 > y0, "reference did not react to the pump"
    assert ll.fundamental_last() > f0, "pump not recorded as fundamental_last for the next advance"
    assert ll.fundamental_ema() == f0, "pump entered the EMA in the same checkpoint"
    # The returned price must be flat (well within 0.1%) despite last now holding the pump.
    assert abs(ret - seeded) <= seeded // 1000, "price_w() return moved with the same-block pump"
    assert (y1 - y0) > 5 * abs(ret - seeded), "price_w() should be far less reactive than the reference"


def test_ll_amm_wash_flash_resistant(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """Wash-trading against AMM.vy itself (crvUSD <-> LP round trips) pumps the AMM's
    fee-inflated equity (x0) and with it YBLendingOracle - but NOT the LL: the pump lives
    entirely in the EMA'd fundamental, and the live shift carries no AMM state. This is the
    vector the fundamental-rate EMA exists for."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)
    yb = lending_oracle

    ll.price_w()                                        # seed at t0
    p0 = ll.price()
    y0 = yb.price_in_asset(yb_lt.address)

    # Same block (no time travel): wash against the AMM, not the pool.
    washer = accounts[5]
    stablecoin._mint_for_testing(washer, 10**6 * 10**18)
    with boa.env.prank(washer):
        stablecoin.approve(yb_amm.address, 2**256 - 1)
        cryptopool.approve(yb_amm.address, 2**256 - 1)
        for _ in range(50):
            got = yb_amm.exchange(0, 1, 20_000 * 10**18, 0)
            yb_amm.exchange(1, 0, got, 0)

    p1 = ll.price()
    y1 = yb.price_in_asset(yb_lt.address)
    print(f"\nLL {p0}->{p1} ({(p1 - p0) / p0 * 100:+.4f}%)   ref {y0}->{y1} ({(y1 - y0) / y0 * 100:+.4f}%)")
    assert (y1 - y0) > y0 // 50, "reference did not react to the AMM wash (expected a big pump)"
    # AMM trades touch neither the pool nor the committed EMA -> the LL price is EXACTLY flat.
    assert p1 == p0, "AMM wash moved the LL price"
    # The pump only enters as fundamental_last (same-block price_w, dt=0 -> EMA unchanged); it
    # will fold into the EMA only over ~ema_time.
    ll.price_w()
    assert ll.fundamental_last() > ll.fundamental_ema()


def test_ll_usd_denomination(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """USD and asset clones track the reference's price_in_usd / price_in_asset to rounding."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll_usd = ll_deployer.deploy()
    ll_usd.initialize(yb_lt.address, True, EMA_TIME, admin)
    ll_asset = _deploy_asset_ll(ll_deployer, yb_lt, admin)

    assert ll_usd.in_usd() is True and ll_asset.in_usd() is False
    _approx(ll_usd.price(), lending_oracle.price_in_usd(yb_lt.address))
    _approx(ll_asset.price(), lending_oracle.price_in_asset(yb_lt.address))
    # BTC ~ $100k here, so the USD price is far larger than the asset (BTC) price.
    assert ll_usd.price() > ll_asset.price()

    _approx(ll_usd.price_w(), lending_oracle.price_in_usd(yb_lt.address))
    assert ll_usd.fundamental_ema() == ll_usd.fundamental_last()
    # Same fundamental for both denominations (it is asset-denominated by construction): seed
    # the asset clone in the same block and compare the recorded live values.
    ll_asset.price_w()
    assert ll_usd.fundamental_last() == ll_asset.fundamental_last()


def test_ll_set_ema_time_access(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer,
):
    """Only the bound factory can retune ema_time, and it is bounds-checked."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)   # factory == admin here
    assert ll.ema_time() == EMA_TIME

    with boa.env.prank(accounts[1]):
        with boa.reverts("Only factory"):
            ll.set_ema_time(1234)
    with boa.env.prank(admin):
        ll.set_ema_time(1234)
        with boa.reverts("ema_time"):
            ll.set_ema_time(0)
    assert ll.ema_time() == 1234

    with boa.reverts("Initialized"):
        ll.initialize(yb_lt.address, False, EMA_TIME, admin)


def test_ll_factory_create_and_retune(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_factory, ll_deployer, lending_oracle,
):
    """Factory spawns a wired USD+asset pair (idempotently) and the DAO can retune their
    ema_time; non-DAO cannot."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    usd, asset = ll_factory.create_oracles(0)
    assert ll_factory.create_oracles(0) == (usd, asset)          # idempotent

    usd_o = ll_deployer.at(usd)
    asset_o = ll_deployer.at(asset)
    assert usd_o.lt_token() == yb_lt.address and asset_o.lt_token() == yb_lt.address
    assert usd_o.in_usd() is True and asset_o.in_usd() is False
    assert usd_o.factory() == ll_factory.address
    assert usd_o.ema_time() == 866 and asset_o.ema_time() == 866
    _approx(usd_o.price(), lending_oracle.price_in_usd(yb_lt.address))
    _approx(asset_o.price(), lending_oracle.price_in_asset(yb_lt.address))

    # Non-DAO cannot retune; the DAO (== admin) can, for the whole pair.
    with boa.env.prank(accounts[1]):
        with boa.reverts("Not DAO"):
            ll_factory.set_ema_time_market(0, 500)
    with boa.env.prank(admin):
        ll_factory.set_ema_time_market(0, 500)
    assert usd_o.ema_time() == 500 and asset_o.ema_time() == 500

    # set_default_ema_time affects only future clones, DAO-gated.
    with boa.env.prank(accounts[1]):
        with boa.reverts("Not DAO"):
            ll_factory.set_default_ema_time(700)
    with boa.env.prank(admin):
        ll_factory.set_default_ema_time(700)
    assert ll_factory.default_ema_time() == 700

    # DAO is transferable; after transfer the old DAO loses access.
    new_dao = accounts[2]
    with boa.env.prank(admin):
        ll_factory.set_dao(new_dao)
    assert ll_factory.dao() == new_dao
    with boa.env.prank(admin):
        with boa.reverts("Not DAO"):
            ll_factory.set_ema_time_market(0, 900)
    with boa.env.prank(new_dao):
        ll_factory.set_ema_time_market(0, 900)
    assert usd_o.ema_time() == 900


# --- equivalence fuzzing: unseeded LL must equal YBLendingOracle in ALL states ---------------
# An unseeded LL prices off the raw current fundamental times the live shift, which is
# algebraically the reference price (the decomposition is exact). So across any pool / AMM /
# agg state the two must agree - including the x0-unsolvable (negative-discriminant) branch,
# where both fall back to the balance-based value. One deliberate exception: when the equity
# marked at price_scale is fully wiped (fundamental == 0) but the price_oracle marking is still
# positive, the LL reports 0 (conservative, undervalues) while the reference stays positive.

def _equiv(a, b, ctx=""):
    # Combined abs+rel tolerance: rel covers normal values; the abs floor covers near-zero
    # prices at the solvency boundary (both ~0). Any real divergence is >> both.
    assert abs(a - b) <= max(10**9, abs(b) // 10**9), f"{ctx}: LL={a} ref={b} diff={a - b}"


def _compare(env, ctx=""):
    asset = env.ll_a.price()
    if asset == 0:
        # Wiped at the price_scale marking (fundamental == 0): the LL reports 0 by design
        # (conservative, never overvalues) while the reference may stay positive. Both LL
        # denominations must agree on 0; skip the equivalence-to-reference check.
        assert env.ll_u.price() == 0, f"usd nonzero while asset wiped {ctx}"
        return
    _equiv(asset, env.yb.price_in_asset(env.lt), f"asset {ctx}")
    _equiv(env.ll_u.price(), env.yb.price_in_usd(env.lt), f"usd {ctx}")


@pytest.fixture
def equiv_env(cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
              yb_allocated, seed_cryptopool, ll_deployer, lending_oracle, mock_agg):
    """A live position plus two UNSEEDED LL clones (asset + usd) and a funded trader. Unseeded so
    each price() reads the raw current virtual_price - directly comparable to the reference."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    lt = yb_lt.address
    ll_a = ll_deployer.deploy()
    ll_a.initialize(lt, False, EMA_TIME, admin)
    ll_u = ll_deployer.deploy()
    ll_u.initialize(lt, True, EMA_TIME, admin)
    trader = accounts[4]
    collateral_token._mint_for_testing(trader, 500 * 10**18)
    stablecoin._mint_for_testing(trader, 500 * 100_000 * 10**18)
    with boa.env.prank(trader):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
    return SimpleNamespace(cryptopool=cryptopool, lt=lt, ll_a=ll_a, ll_u=ll_u,
                           yb=lending_oracle, trader=trader, admin=admin, mock_agg=mock_agg)


def _apply_op(env, op, param):
    cp = env.cryptopool
    try:
        with boa.env.prank(env.trader):
            if op == 0:                                   # price up (buy collateral)
                cp.exchange(0, 1, 20000 * 10**18, 0)
            elif op == 1:                                 # small price down
                cp.exchange(1, 0, 5 * 10**17, 0)
            elif op == 2:                                 # crash (toward insolvency)
                cp.exchange(1, 0, 15 * 10**18, 0)
            elif op == 3:                                 # wash -> fees raise virtual_price
                got = cp.exchange(0, 1, 5000 * 10**18, 0)
                cp.exchange(1, 0, got, 0)
    except Exception:
        pass                                              # over-sized trade / insolvent: skip
    if op == 4:                                           # move the crvUSD aggregator (0.70..1.30)
        with boa.env.prank(env.admin):
            env.mock_agg.set_price(param * 10**16)
    boa.env.time_travel(max(1, param) * 60)               # let the pool price_oracle EMA advance


@given(seq=st.lists(st.tuples(st.integers(0, 5), st.integers(55, 130)), min_size=1, max_size=6))
@settings(max_examples=FUZZ_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_ll_equiv_fuzz(equiv_env, seq):
    """Drive random price moves / fee accrual / agg changes / idle gaps and assert the unseeded
    LL equals the reference at every step (each example rolled back via anchor)."""
    with boa.env.anchor():
        _compare(equiv_env, "initial")
        for i, (op, param) in enumerate(seq):
            _apply_op(equiv_env, op, param)
            _compare(equiv_env, f"step{i} op{op} p{param}")


def test_ll_equiv_insolvent(equiv_env):
    """Drive the AMM position x0-unsolvable (the reverting-discriminant branch) via a crvUSD
    depeg - low agg shrinks the USD collateral value below the leverage threshold - then assert
    the LL still equals the reference there (both fall back to the balance-based value)."""
    env = equiv_env
    with boa.env.anchor():
        reached = False
        for agg_pct in (85, 80, 70, 60, 50, 40, 30):
            with boa.env.prank(env.admin):
                env.mock_agg.set_price(agg_pct * 10**16)
            # In the x0-unsolvable branch the reference is forced onto the balance path, so the
            # auto (use_balances=False) and forced (True) values coincide - our branch detector.
            if env.yb.price_in_asset(env.lt, False) == env.yb.price_in_asset(env.lt, True):
                reached = True
                break
        assert reached, "did not reach the x0-unsolvable branch"
        _compare(env, "insolvent")
