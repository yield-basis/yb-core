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
  - USD vs asset denomination: the USD forwarder shares the asset oracle's ONE EMA, so a
    checkpoint from either side advances it (ChainSecurity #002),
  - factory-settable ema_time,
  - untradable AMM (x0 unsolvable): both price the balance value (what LT.emergency_withdraw
    pays) and checkpoints there record no fundamental; the reference's staked prices work there.
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


@pytest.fixture
def ll(ll_deployer, yb_lt, admin):
    """A fresh, unseeded asset LL on the test market (factory role: admin)."""
    ll = ll_deployer.deploy()
    ll.initialize(yb_lt.address, EMA_TIME, admin)
    return ll


@pytest.fixture
def ll_usd(ll_usd_deployer, ll):
    """The USD forwarder onto `ll`, sharing its EMA."""
    return ll_usd_deployer.deploy(ll.address)


def test_ll_equivalence_to_reference(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle,
):
    """Unseeded and freshly-seeded (fundamental_ema == current fundamental), the LL equals
    YBLendingOracle to rounding - the decomposed math must reproduce the reference."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    yb = lending_oracle

    ref = yb.price_in_asset(yb_lt.address)
    _approx(ll.price(), ref)               # unseeded: raw fundamental
    _approx(ll.price_w(), ref)             # seed
    assert ll.fundamental_ema() == ll.fundamental_last()  # seeded: ema == last == current
    _approx(ll.price(), ref)               # settled: ema == current fundamental


def test_ll_fundamental_ema_smoothing(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle,
):
    """A fundamental bump (pool-fee wash raises virtual_price -> x0) is folded into the EMA
    only after it survives a checkpoint, then converges over ~ema_time - the honest 'value
    must survive a later block' property."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)

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
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle,
):
    """A virtual_price pump within a single block moves YBLendingOracle (reads current vprice)
    but NOT the LL, which prices off the committed EMA."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
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
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle,
):
    """price_w() records the current fundamental as fundamental_last BEFORE it prices, so it
    MUST price off the smoothed (old) EMA it just computed - never the freshly-stored last. A
    same-block pump that precedes price_w() must not move its RETURN value; the pump is only
    recorded for the NEXT advance. Guards the save-before-price ordering in price_w()."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
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
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle,
):
    """Wash-trading against AMM.vy itself (crvUSD <-> LP round trips) pumps the AMM's
    fee-inflated equity (x0) and with it YBLendingOracle - but NOT the LL: the pump lives
    entirely in the EMA'd fundamental, and the live shift carries no AMM state. This is the
    vector the fundamental-rate EMA exists for."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
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
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin, yb_allocated,
    seed_cryptopool, ll, ll_usd, lending_oracle, mock_agg,
):
    """The USD forwarder (== price_usd()) tracks the reference's price_in_usd to rounding, and is
    exactly the asset price times the live asset price (price_oracle * agg)."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    assert ll_usd.ORACLE() == ll.address

    _approx(ll_usd.price(), lending_oracle.price_in_usd(yb_lt.address))
    _approx(ll.price(), lending_oracle.price_in_asset(yb_lt.address))
    assert ll_usd.price() == ll.price_usd()
    # BTC ~ $100k here, so the USD price is far larger than the asset (BTC) price.
    assert ll_usd.price() > ll.price()
    asset_price = cryptopool.price_oracle() * mock_agg.price() // 10**18
    assert ll.price_usd() == ll.price() * asset_price // 10**18

    _approx(ll_usd.price_w(), lending_oracle.price_in_usd(yb_lt.address))
    assert ll.fundamental_ema() > 0, "USD checkpoint did not seed the shared EMA"
    assert ll.fundamental_ema() == ll.fundamental_last()


def test_ll_usd_shares_ema(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin, yb_allocated,
    seed_cryptopool, ll, ll_usd, mock_agg,
):
    """USD and asset consumers checkpoint ONE EMA: a fundamental bump recorded by an asset-side
    price_w() is folded in by a later USD-side price_w(), and both denominations then price off
    the same advanced EMA (ChainSecurity #002)."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)

    ll_usd.price_w()                                    # seed via the USD side
    f0 = ll.fundamental_ema()
    assert f0 > 0 and f0 == ll.fundamental_last()

    _bump_vprice(cryptopool, collateral_token, stablecoin, accounts)
    boa.env.time_travel(60)
    ll.price_w()                                        # asset side records the bump
    f1 = ll.fundamental_last()
    assert f1 > f0, "wash did not raise the fundamental"
    assert ll.fundamental_ema() == f0, "bump entered the EMA before surviving a checkpoint"

    boa.env.time_travel(HALF_LIFE)
    ret = ll_usd.price_w()                              # USD side folds it in
    frac = (ll.fundamental_ema() - f0) / (f1 - f0)
    assert 0.45 < frac < 0.55, f"USD checkpoint did not advance the shared EMA (frac={frac:.3f})"

    # Same block: the asset price reads the EMA the USD checkpoint just advanced.
    asset_price = cryptopool.price_oracle() * mock_agg.price() // 10**18
    assert ret == ll.price() * asset_price // 10**18
    assert ret == ll_usd.price()


def test_ll_set_ema_time_access(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll,
):
    """Only the bound factory can retune ema_time, and it is bounds-checked."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
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
        ll.initialize(yb_lt.address, EMA_TIME, admin)


def test_ll_factory_create_and_retune(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin, yb_allocated,
    seed_cryptopool, ll_factory, ll_deployer, ll_usd_deployer, lending_oracle,
):
    """Factory spawns a wired asset oracle + USD forwarder to it (idempotently) and the DAO can
    retune the market's (shared) ema_time; non-DAO cannot."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    usd, asset = ll_factory.create_oracles(0)
    assert ll_factory.create_oracles(0) == (usd, asset)          # idempotent
    assert ll_factory.usd_oracle(0) == usd and ll_factory.asset_oracle(0) == asset

    usd_o = ll_usd_deployer.at(usd)
    asset_o = ll_deployer.at(asset)
    assert usd_o.ORACLE() == asset                               # USD side forwards to the EMA owner
    assert asset_o.lt_token() == yb_lt.address
    assert asset_o.factory() == ll_factory.address
    assert asset_o.ema_time() == 866
    _approx(usd_o.price(), lending_oracle.price_in_usd(yb_lt.address))
    _approx(asset_o.price(), lending_oracle.price_in_asset(yb_lt.address))

    # Non-DAO cannot retune; the DAO (== admin) can, once for both denominations.
    with boa.env.prank(accounts[1]):
        with boa.reverts("Not DAO"):
            ll_factory.set_ema_time(0, 500)
    with boa.env.prank(admin):
        ll_factory.set_ema_time(0, 500)
        with boa.reverts("No oracles"):
            ll_factory.set_ema_time(1, 500)
    assert asset_o.ema_time() == 500

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
            ll_factory.set_ema_time(0, 900)
    with boa.env.prank(new_dao):
        ll_factory.set_ema_time(0, 900)
    assert asset_o.ema_time() == 900


# --- untradable AMM (x0 unsolvable) ------------------------------------------------------------
# A crvUSD depeg (low agg) shrinks the USD collateral value below the leverage threshold.

def _tradable(amm):
    try:
        amm.value_oracle()
        return True
    except Exception:
        return False


def _set_agg(mock_agg, admin, price):
    with boa.env.prank(admin):
        mock_agg.set_price(price)


def _agg_boundary(amm, mock_agg, admin):
    """(lo, hi): agg prices 1 wei apart with the AMM untradable at lo and tradable at hi."""
    lo, hi = 10**16, 10**18
    _set_agg(mock_agg, admin, hi)
    assert _tradable(amm)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        _set_agg(mock_agg, admin, mid)
        if _tradable(amm):
            hi = mid
        else:
            lo = mid
    return lo, hi


def _near(price, balance):
    # The LL's balance value rounds differently from the reference's (cv_ps * pv_norm vs
    # portfolio_value * D / supply): a few wei either way.
    return abs(price - balance) <= balance // 10**12 + 10


def test_ll_untradable_boundary(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle, mock_agg,
):
    """Crossing into the untradable region both oracles move to the balance value, what
    LT.emergency_withdraw pays once the AMM can't trade. That is a real jump up: just above
    the boundary arbitrage would still rebalance the AMM at LT holders' cost (realised values on
    both sides: test_lending_oracle_realised). A settled LL takes the same jump."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    lt = yb_lt.address

    lo, hi = _agg_boundary(yb_amm, mock_agg, admin)
    _set_agg(mock_agg, admin, hi * 1001 // 1000)
    for _ in range(12):
        ll.price_w()
        boa.env.time_travel(EMA_TIME)
    lo, hi = _agg_boundary(yb_amm, mock_agg, admin)         # interest has moved it

    _set_agg(mock_agg, admin, hi)
    ll_t, ref_t = ll.price(), lending_oracle.price_in_asset(lt)
    _set_agg(mock_agg, admin, lo)
    ll_u, ref_u = ll.price(), lending_oracle.price_in_asset(lt)
    balance = lending_oracle.price_in_asset(lt, True)
    print(f"\nLL {ll_t} -> {ll_u}   ref {ref_t} -> {ref_u} ({(ref_u - ref_t) / ref_t * 100:+.2f}%)")

    assert ref_u == balance
    assert _near(ll_u, balance)
    assert ref_u > ref_t * 105 // 100 and ll_u > ll_t * 105 // 100


def test_ll_untradable_balance_value(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll, ll_usd,
    lending_oracle, mock_agg,
):
    """F-01: seeded on a healthy market, once the AMM is untradable the LL prices exactly the
    balance value, as the reference does - in either denomination, on price_w(), and without
    checkpoints (no blow-up as cv_ps -> debt, no drop to 0 while there is equity)."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    lt = yb_lt.address
    ll.price_w()
    boa.env.time_travel(60)
    ll.price_w()

    checked = 0
    for agg_pct in (80, 70, 60, 55, 50, 45, 40, 35, 30, 25, 20):
        _set_agg(mock_agg, admin, agg_pct * 10**16)
        if _tradable(yb_amm):
            continue
        balance = lending_oracle.price_in_asset(lt, True)
        price = ll.price()
        print(f"agg {agg_pct}%: LL {price} balance {balance}")
        assert _near(price, balance)
        assert (price > 0) == (balance > 0)
        assert _near(ll_usd.price(), lending_oracle.price_in_usd(lt, True))
        assert lending_oracle.price_in_asset(lt) == balance
        with boa.env.anchor():
            assert _near(ll.price_w(), balance)
            boa.env.time_travel(3 * 86400)
            assert _near(ll.price(), lending_oracle.price_in_asset(lt, True))
        checked += 1
    assert checked >= 3, "did not reach the untradable region"


def test_ll_untradable_checkpoints(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll, lending_oracle, mock_agg,
):
    """Untradable checkpoints record no fundamental: an unseeded LL stays unseeded and prices
    at the balance value; a seeded one keeps its last tradable fundamental."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    lt = yb_lt.address
    lo, _ = _agg_boundary(yb_amm, mock_agg, admin)

    _set_agg(mock_agg, admin, lo * 9 // 10)
    _equiv(ll.price(), lending_oracle.price_in_asset(lt, True), "unseeded")
    _equiv(ll.price_w(), lending_oracle.price_in_asset(lt, True), "unseeded price_w")
    assert ll.fundamental_ema() == 0 and ll.fundamental_last() == 0

    _set_agg(mock_agg, admin, 10**18)
    boa.env.time_travel(60)
    ll.price_w()
    f0 = ll.fundamental_last()
    assert f0 > 0 and ll.fundamental_ema() == f0

    _set_agg(mock_agg, admin, lo * 9 // 10)
    boa.env.time_travel(60)
    ll.price_w()
    assert ll.fundamental_last() == f0 and ll.fundamental_ema() == f0
    assert ll.ema_ts() == boa.env.evm.patch.timestamp


def test_ref_staked_price_untradable(
    cryptopool, yb_lt, yb_amm, yb_staker, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, lending_oracle, mock_agg,
):
    """F-07: the reference's staked prices no longer go through LT.updated_balances(), which
    reverts while the AMM is untradable. They match the gauge's own conversion while tradable,
    and use the stored staker balance once untradable (as the balance value uses the stored
    liquidity values), with or without use_balances."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    with boa.env.prank(admin):
        yb_staker.deposit(yb_lt.balanceOf(admin) // 2, admin)
    lt = yb_lt.address
    lo, hi = _agg_boundary(yb_amm, mock_agg, admin)

    def scaled(price, staker_balance):
        return price * ((staker_balance + 1) * 10**18 // (yb_staker.totalSupply() + 1)) // 10**18

    _set_agg(mock_agg, admin, hi)
    _approx(lending_oracle.staked_price_in_asset(lt),
            scaled(lending_oracle.price_in_asset(lt), yb_lt.updated_balances()[1]))

    _set_agg(mock_agg, admin, lo)
    with boa.reverts():
        yb_lt.updated_balances()
    for agg_pct in (100, 90, 80, 70, 60, 50, 40, 30):
        agg = lo * agg_pct // 100
        _set_agg(mock_agg, admin, agg)
        assert not _tradable(yb_amm)
        for use_balances in (False, True):
            price = lending_oracle.price_in_asset(lt, use_balances)
            staked = lending_oracle.staked_price_in_asset(lt, use_balances)
            _approx(staked, scaled(price, yb_lt.balanceOf(yb_staker.address)))
            _approx(lending_oracle.staked_price_in_usd(lt, use_balances),
                    staked * (cryptopool.price_oracle() * agg // 10**18) // 10**18, rel=10**-6)


# --- equivalence fuzzing: unseeded LL must equal YBLendingOracle in ALL states ---------------
# An unseeded LL prices off the raw current fundamental times the live shift, which is
# algebraically the reference price (the decomposition is exact). So across any pool / AMM /
# agg state the two must agree - including the x0-unsolvable (negative-discriminant) branch,
# where both use the balance value at price_oracle.

def _equiv(a, b, ctx=""):
    # Combined abs+rel tolerance: rel covers normal values; the abs floor covers near-zero
    # prices at the solvency boundary (both ~0). Any real divergence is >> both.
    assert abs(a - b) <= max(10**9, abs(b) // 10**9), f"{ctx}: LL={a} ref={b} diff={a - b}"


def _compare(env, ctx=""):
    _equiv(env.ll_a.price(), env.yb.price_in_asset(env.lt), f"asset {ctx}")
    _equiv(env.ll_u.price(), env.yb.price_in_usd(env.lt), f"usd {ctx}")


@pytest.fixture
def equiv_env(cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
              yb_allocated, seed_cryptopool, ll_deployer, ll_usd_deployer, lending_oracle, mock_agg):
    """A live position plus an UNSEEDED LL, its USD forwarder and a funded trader. Unseeded so
    each price() reads the raw current virtual_price - directly comparable to the reference."""
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    lt = yb_lt.address
    ll_a = ll_deployer.deploy()
    ll_a.initialize(lt, EMA_TIME, admin)
    ll_u = ll_usd_deployer.deploy(ll_a.address)
    trader = accounts[4]
    collateral_token._mint_for_testing(trader, 500 * 10**18)
    stablecoin._mint_for_testing(trader, 500 * 100_000 * 10**18)
    with boa.env.prank(trader):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
    return SimpleNamespace(cryptopool=cryptopool, amm=yb_amm, lt=lt, ll_a=ll_a, ll_u=ll_u,
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
    the LL still equals the reference there (both price the balance value)."""
    env = equiv_env
    with boa.env.anchor():
        checked = 0
        for agg_pct in range(90, 10, -5):
            _set_agg(env.mock_agg, env.admin, agg_pct * 10**16)
            if _tradable(env.amm):
                continue
            _compare(env, f"agg {agg_pct}%")
            assert env.yb.price_in_asset(env.lt) == env.yb.price_in_asset(env.lt, True)
            checked += 1
        assert checked >= 3, "did not reach the x0-unsolvable branch"
