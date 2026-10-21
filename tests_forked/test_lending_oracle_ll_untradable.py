"""
YBLendingOracleLL on real markets pushed into the untradable region (x0 unsolvable, F-01).

The AMM position is moved by overwriting AMM.collateral_amount on a head fork, with
k = cv_ps / (16/9 * debt): x0 is unsolvable when k < 1, and cv_ps == debt at k = 0.5625.
A fresh LL is seeded on the healthy market first, as a deployed one would be. Checks:
  - LL and YBLendingOracle price the balance value (what LT.emergency_withdraw pays, as the AMM
    can't trade), positive while it is, including as cv_ps -> debt (where the old seeded LL
    blew up),
  - across the boundary with no arbitrage in between, both step up to it from the value after
    arbitrage (the arbitrage still pending there), as the realised value does,
  - after a shock: the balance value through checkpoints and a long idle gap,
  - on recovery after a day untradable, the LL lands on the reference's value after
    arbitrage (its EMA followed the position), not on the stale pre-untradable rate,
  - YBLendingOracle's staked prices work there (F-07).
"""
from fractions import Fraction

import boa
import pytest
from tests_forked.networks import NETWORK

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
MARKETS = [7, 8, 9, 10]
EMA_TIME = 866
COLLATERAL_SLOT = 1  # AMM.collateral_amount

GRID = ["0.999", "0.95", "0.9", "0.8", "0.7", "0.65", "0.6", "0.58", "0.565", "0.5626", "0.55", "0.5"]
EPS_USD = [10**24, 10**21, 10**18, 1]  # cv_ps - debt


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier="latest"):
        yield


@pytest.fixture(scope="module")
def ref():
    proxy = boa.load_partial("contracts/utils/YBPriceProxy.vy").deploy()
    return boa.load("contracts/utils/YBLendingOracle.vy", FACTORY, proxy.address)


@pytest.fixture(scope="module")
def deployers():
    return {
        "factory": boa.load_partial("contracts/Factory.vy"),
        "lt": boa.load_partial("contracts/LT.vy"),
        "amm": boa.load_partial("contracts/AMM.vy"),
        "lp_oracle": boa.load_partial("contracts/CryptopoolLPOracle.vy"),
        "ll": boa.load_partial("contracts/utils/YBLendingOracleLL.vy"),
        "gauge": boa.load_partial("contracts/dao/LiquidityGauge.vy"),
    }


@pytest.fixture(params=MARKETS, ids=[f"m{i}" for i in MARKETS])
def market(request, deployers):
    m = deployers["factory"].at(FACTORY).markets(request.param)
    lt = deployers["lt"].at(m.lt)
    amm = deployers["amm"].at(m.amm)
    ll = deployers["ll"].deploy()
    ll.initialize(lt.address, EMA_TIME, FACTORY)
    ll.price_w()                                         # seed on the healthy market
    boa.env.time_travel(12)
    ll.price_w()
    return lt, amm, ll, deployers["lp_oracle"].at(amm.PRICE_ORACLE_CONTRACT())


def _set_k(amm, lp_oracle, k):
    k = Fraction(k)
    coll = 16 * k.numerator * amm.get_debt() * 10**18 // (9 * k.denominator * lp_oracle.price())
    boa.env.set_storage(amm.address, COLLATERAL_SLOT, coll)


def _set_equity(amm, lp_oracle, eps):
    coll = (amm.get_debt() + eps) * 10**18 // lp_oracle.price() + 1
    boa.env.set_storage(amm.address, COLLATERAL_SLOT, coll)


def _tradable(amm):
    try:
        amm.value_oracle()
        return True
    except Exception:
        return False


def _near(price, balance):
    # The LL's balance value rounds differently from the reference's: a few wei either way.
    return abs(price - balance) <= balance // 10**12 + 10


def _check_balance(lt, ll, ref, ctx):
    balance = ref.price_in_asset(lt.address, True)
    price = ll.price()
    assert _near(price, balance), f"{ctx}: LL {price} balance {balance}"
    assert (price > 0) == (balance > 0), f"{ctx}: LL {price} balance {balance}"
    assert _near(ll.price_usd(), ref.price_in_usd(lt.address, True)), ctx
    assert ref.price_in_asset(lt.address) == balance, ctx
    return price, balance


def test_untradable_balance_value(market, ref):
    lt, amm, ll, lp_oracle = market
    for k in GRID:
        with boa.env.anchor():
            _set_k(amm, lp_oracle, k)
            assert not _tradable(amm)
            price, balance = _check_balance(lt, ll, ref, f"k={k}")
            print(f"k={k}: LL {price / 1e18:.6f} balance {balance / 1e18:.6f}")
    for eps in EPS_USD:
        with boa.env.anchor():
            _set_equity(amm, lp_oracle, eps)
            _check_balance(lt, ll, ref, f"cv_ps - debt = {eps}")


def test_untradable_boundary(market, ref):
    lt, amm, ll, lp_oracle = market
    _set_k(amm, lp_oracle, "1.002")
    for _ in range(12):
        ll.price_w()
        boa.env.time_travel(EMA_TIME)

    _set_k(amm, lp_oracle, "1.00000000000001")
    assert _tradable(amm)
    ll_t, ref_t = ll.price(), ref.price_in_asset(lt.address)
    _set_k(amm, lp_oracle, "0.99999999999999")
    assert not _tradable(amm)
    ll_u, ref_u = ll.price(), ref.price_in_asset(lt.address)
    print(f"LL {ll_t / 1e18:.6f} -> {ll_u / 1e18:.6f}, reference {ref_t / 1e18:.6f} -> {ref_u / 1e18:.6f}")

    balance = ref.price_in_asset(lt.address, True)
    assert ref_u == balance and _near(ll_u, balance)
    assert ref_u > ref_t * 105 // 100 and ll_u > ll_t * 105 // 100


def test_untradable_recovery(market, ref):
    lt, amm, ll, lp_oracle = market
    for _ in range(36):                                  # settled on the healthy market
        boa.env.time_travel(600)
        ll.price_w()
    _set_k(amm, lp_oracle, "0.99")
    for _ in range(144):                                 # a day, checkpointed
        boa.env.time_travel(600)
        assert _near(ll.price_w(), ref.price_in_asset(lt.address, True))

    # Recovered by hand: the 1% of collateral added back is a jump in the fundamental, which
    # the EMA lags from below.
    _set_k(amm, lp_oracle, "1.00000000000001")
    assert _tradable(amm)
    ll_t, ref_t = ll.price(), ref.price_in_asset(lt.address)
    print(f"recovered: LL {ll_t / 1e18:.6f} reference {ref_t / 1e18:.6f} ({ll_t / ref_t:.4f})")
    assert ref_t * 98 // 100 <= ll_t <= ref_t + ref_t // 10**9
    for _ in range(15):
        boa.env.time_travel(600)
        ll.price_w()
        assert ll.price() <= ref.price_in_asset(lt.address) * (10**9 + 1) // 10**9
    assert abs(ll.price() - ref.price_in_asset(lt.address)) <= ref_t // 10**4


@pytest.mark.parametrize("k", ["0.9", "0.6"])
def test_untradable_shock(market, ref, k):
    lt, amm, ll, lp_oracle = market
    _set_k(amm, lp_oracle, k)
    with boa.env.anchor():
        boa.env.time_travel(3 * 86400)                   # no checkpoints at all
        _check_balance(lt, ll, ref, f"k={k} idle")
    for dt in [12] + [60] * 10 + [300] * 6 + [1800] * 2:
        boa.env.time_travel(dt)
        price = ll.price_w()
        assert _near(price, ref.price_in_asset(lt.address, True)), f"k={k} after {dt}s"
        _check_balance(lt, ll, ref, f"k={k} after {dt}s")


def test_untradable_staked_price(market, ref, deployers):
    """F-07: staked prices no longer revert while the AMM is untradable; there they use the
    stored staker balance, as the balance value uses the stored liquidity values."""
    lt, amm, ll, lp_oracle = market
    gauge = deployers["gauge"].at(lt.staker())
    scale = (lt.balanceOf(gauge.address) + 1) * 10**18 // (gauge.totalSupply() + 1)
    for k in GRID:
        with boa.env.anchor():
            _set_k(amm, lp_oracle, k)
            assert not _tradable(amm)
            expected = ref.price_in_asset(lt.address, True) * scale // 10**18
            for use_balances in (False, True):
                staked = ref.staked_price_in_asset(lt.address, use_balances)
                assert abs(staked - expected) <= 2 + expected // 10**12, f"k={k}: {staked} vs {expected}"
                ref.staked_price_in_usd(lt.address, use_balances)
