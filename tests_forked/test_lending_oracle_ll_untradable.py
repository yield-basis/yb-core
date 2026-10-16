"""
YBLendingOracleLL on real markets pushed into the untradable region (x0 unsolvable, F-01).

The AMM position is moved by overwriting AMM.collateral_amount on a head fork, with
k = cv_ps / (16/9 * debt): x0 is unsolvable when k < 1, and cv_ps == debt at k = 0.5625.
A fresh LL is seeded on the healthy market first, as a deployed one would be. Checks:
  - LL and YBLendingOracle never above the balance value, positive while it is,
    including as cv_ps -> debt (where the old seeded LL blew up),
  - both continuous across the boundary (the LL once its EMA has settled),
  - after a shock: at or below the balance value through checkpoints and a long idle gap.
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


def _rounded_up(balance):
    # The LL's balance value rounds differently from the reference's: a few wei either way.
    return balance + balance // 10**12 + 10


def _check_capped(lt, ll, ref, ctx):
    balance = ref.price_in_asset(lt.address, True)
    price = ll.price()
    assert price <= _rounded_up(balance), f"{ctx}: LL {price} above balance {balance}"
    assert (price > 0) == (balance > 0), f"{ctx}: LL {price} balance {balance}"
    assert ll.price_usd() <= _rounded_up(ref.price_in_usd(lt.address, True)), ctx
    ref_price = ref.price_in_asset(lt.address)
    assert ref_price <= balance, f"{ctx}: reference {ref_price} above balance {balance}"
    assert (ref_price > 0) == (balance > 0), f"{ctx}: reference {ref_price} balance {balance}"
    return price, balance


def test_untradable_capped(market, ref):
    lt, amm, ll, lp_oracle = market
    for k in GRID:
        with boa.env.anchor():
            _set_k(amm, lp_oracle, k)
            assert not _tradable(amm)
            price, balance = _check_capped(lt, ll, ref, f"k={k}")
            print(f"k={k}: LL {price / 1e18:.6f} balance {balance / 1e18:.6f}")
    for eps in EPS_USD:
        with boa.env.anchor():
            _set_equity(amm, lp_oracle, eps)
            _check_capped(lt, ll, ref, f"cv_ps - debt = {eps}")


def test_untradable_continuous(market, ref):
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
    assert ll_u == ll_t
    assert ll_u < balance
    assert abs(ref_u - ref_t) <= ref_t // 10**6, "reference jumped at the boundary"
    assert ref_u < balance


@pytest.mark.parametrize("k", ["0.9", "0.6"])
def test_untradable_shock(market, ref, k):
    lt, amm, ll, lp_oracle = market
    _set_k(amm, lp_oracle, k)
    with boa.env.anchor():
        boa.env.time_travel(3 * 86400)                   # no checkpoints at all
        _check_capped(lt, ll, ref, f"k={k} idle")
    for dt in [12] + [60] * 10 + [300] * 6 + [1800] * 2:
        boa.env.time_travel(dt)
        price = ll.price_w()
        assert price <= _rounded_up(ref.price_in_asset(lt.address, True)), f"k={k} after {dt}s"
        _check_capped(lt, ll, ref, f"k={k} after {dt}s")
