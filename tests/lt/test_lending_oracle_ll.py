"""
YBLendingOracleLL: ybLT oracle with an honest EMA on the pool virtual_price.

The LP-oracle math is reproduced in-contract (as in YBNetPressure), which separates the
manipulation-resistant price frame (price_oracle/price_scale - itself the cryptopool's EMA)
from the fee level, which enters ONLY through virtual_price. The LL applies a cryptopool-style
EMA to virtual_price, blending the PREVIOUS checkpoint's value (never the current-block one),
so it is flash-proof. Checks:
  - unseeded / settled LL equals YBLendingOracle (the untouched reference) to rounding,
  - the virtual_price EMA lags a fee bump and converges over ~ema_time,
  - a same-block virtual_price pump does NOT move the LL price (flash resistance),
  - USD vs asset denomination, factory-settable ema_time.
"""
from types import SimpleNamespace

import boa
import pytest
from hypothesis import given, settings, strategies as st, HealthCheck

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
    """Unseeded and freshly-seeded (vp_ema == current vprice), the LL equals YBLendingOracle to
    rounding - the reformulated in-contract math must reproduce the reference."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)
    yb = lending_oracle

    ref = yb.price_in_asset(yb_lt.address)
    _approx(ll.price(), ref)               # unseeded: raw virtual_price
    _approx(ll.price_w(), ref)             # seed
    assert ll.vp_ema() == cryptopool.virtual_price()   # seeded at the current vprice
    _approx(ll.price(), ref)               # settled: vp_ema == current vprice


def test_ll_vp_ema_smoothing(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    """A virtual_price bump is folded into the EMA only after it survives a checkpoint, then
    converges over ~ema_time - the honest 'value must survive a later block' property."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = _deploy_asset_ll(ll_deployer, yb_lt, admin)

    ll.price_w()                                        # seed: vp_ema = vp_last = vp0
    vp0 = cryptopool.virtual_price()
    assert ll.vp_ema() == vp0

    _bump_vprice(cryptopool, collateral_token, stablecoin, accounts)
    vp1 = cryptopool.virtual_price()
    assert vp1 > vp0, "wash did not raise virtual_price"

    # Checkpoint #2: advances using the OLD vp_last (== vp0), so vp_ema is unchanged; the bump
    # is only now recorded as vp_last for the next advance.
    boa.env.time_travel(EMA_TIME)
    ll.price_w()
    assert ll.vp_ema() == vp0, "bump entered the EMA before surviving a checkpoint"

    # Checkpoint #3 one half-life later: vp_ema moves ~halfway from vp0 to vp1.
    boa.env.time_travel(HALF_LIFE)
    ll.price_w()
    frac = (ll.vp_ema() - vp0) / (vp1 - vp0)
    print(f"\nvp0={vp0} vp1={vp1} half-life frac={frac:.3f}")
    assert 0.45 < frac < 0.55

    # Many time-constants later: converged to vp1.
    boa.env.time_travel(EMA_TIME * 8)
    ll.price_w()
    _approx(ll.vp_ema(), vp1, rel=10**-3)


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
    assert ll_usd.vp_ema() == cryptopool.virtual_price()


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
# An unseeded LL prices off the raw current virtual_price, exactly like the reference (which
# uses D/totalSupply, == the vprice-derived level by the on-chain identity). So across any pool
# / AMM / agg state the two must agree - including the x0-unsolvable (negative-discriminant)
# branch, where both fall back to the balance-based value.

def _equiv(a, b, ctx=""):
    # Combined abs+rel tolerance: rel covers normal values; the abs floor covers near-zero
    # prices at the solvency boundary (both ~0). Any real divergence is >> both.
    assert abs(a - b) <= max(10**9, abs(b) // 10**9), f"{ctx}: LL={a} ref={b} diff={a - b}"


def _compare(env, ctx=""):
    _equiv(env.ll_a.price(), env.yb.price_in_asset(env.lt), f"asset {ctx}")
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
@settings(max_examples=15, deadline=None,
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
        _equiv(env.ll_a.price(), env.yb.price_in_asset(env.lt), "insolvent asset")
        _equiv(env.ll_u.price(), env.yb.price_in_usd(env.lt), "insolvent usd")
