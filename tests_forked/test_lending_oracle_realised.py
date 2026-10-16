"""
YBLendingOracle and YBLendingOracleLL against what an LT position actually realises, on the
real markets 7-10 at the chain head (see tests/lt/test_lending_oracle_realised.py for the
full grid on a test pool).

price_oracle is let to settle to spot (no trades), the AMM position is set (collateral
overwritten, k = collateral value / (16/9 debt)), a rational arbitrageur trades the AMM towards
the market LP price as far as AMM.exchange allows (buying LP: the AMM is at or below its
balance point here and the markets are at or above price_scale), then 1% of a position exits
with emergency_withdraw. Compared in USD with the oracle readings taken before the arbitrage;
the oracles count crvUSD debt at $1 while the exit is marked at agg, hence the slack above.
"""
from fractions import Fraction

import boa
import pytest
from tests_forked.networks import NETWORK

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
MARKETS = [7, 8, 9, 10]
EMA_TIME = 866
COLLATERAL_SLOT = 1  # AMM.collateral_amount
K_GRID = ["1.125", "1.06", "1.03", "1.001", "0.999", "0.9", "0.7"]
SHORTFALL = 10**16   # 1%: AMM fees earned on the arbitrage
EXCESS = 3 * 10**15  # 0.3%: crvUSD debt at $1 in the oracles vs agg in the exit
CRVUSD_ABI = """[
 {"name":"minter","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"},
 {"name":"mint","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"to"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"}
]"""
PRICE_ABI = '[{"name":"price","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'
DECIMALS_ABI = '[{"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"}]'


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
        "pool": boa.load_partial("contracts/twocrypto_pool/contracts/main/Twocrypto.vy"),
        "ll": boa.load_partial("contracts/utils/YBLendingOracleLL.vy"),
        "probe": boa.load_partial("contracts/testing/YBOracleRatioProbe.vy"),
    }


def _tradable(amm):
    try:
        amm.value_oracle()
        return True
    except Exception:
        return False


def _buy_lp(amm, target, arb):
    """Rational arbitrage buying LP from an AMM priced below the market LP price `target`:
    the largest trade AMM.exchange accepts while its marginal price plus fee stays below target
    and the trade makes money. Returns the crvUSD spent."""
    fee = amm.fee()
    lo, hi = 0, amm.get_debt()
    for _ in range(64):
        mid = (lo + hi) // 2
        with boa.env.anchor():
            try:
                with boa.env.prank(arb):
                    out = amm.exchange(0, 1, mid, 0)
                ok = amm.get_p() * 10**18 <= target * (10**18 - fee) and out * target >= mid * 10**18
            except Exception:
                ok = False
        if ok:
            lo = mid
        else:
            hi = mid
    if lo > 0:
        with boa.env.prank(arb):
            amm.exchange(0, 1, lo, 0)
    return lo


@pytest.mark.parametrize("market_id", MARKETS, ids=[f"m{i}" for i in MARKETS])
def test_oracles_match_realised_value(ref, deployers, market_id):
    m = deployers["factory"].at(FACTORY).markets(market_id)
    lt = deployers["lt"].at(m.lt)
    amm = deployers["amm"].at(m.amm)
    pool = deployers["pool"].at(m.cryptopool)
    probe = deployers["probe"].deploy()
    lp_oracle = boa.loads_abi(PRICE_ABI).at(amm.PRICE_ORACLE_CONTRACT())
    agg = boa.loads_abi(PRICE_ABI).at(lt.agg())
    ll = deployers["ll"].deploy()
    ll.initialize(lt.address, EMA_TIME, FACTORY)                     # unseeded: raw fundamental
    crvusd = boa.loads_abi(CRVUSD_ABI).at(CRVUSD)
    asset_scale = 10**(18 - boa.loads_abi(DECIMALS_ABI).at(m.asset_token).decimals())   # WBTC, cbBTC: 8

    user, arb = boa.env.generate_address(), boa.env.generate_address()
    shares = lt.balanceOf(m.staker) // 1000
    with boa.env.prank(m.staker):
        lt.transfer(user, shares)
    with boa.env.prank(crvusd.minter()):
        crvusd.mint(user, 10**27)
        crvusd.mint(arb, 10**27)
    with boa.env.prank(user):
        crvusd.approve(lt.address, 2**256 - 1)                       # emergency_withdraw may need crvUSD
    with boa.env.prank(arb):
        crvusd.approve(amm.address, 2**256 - 1)
    boa.env.time_travel(86400)                                       # price_oracle settles to spot
    spot = pool.last_prices()
    assert abs(spot - pool.price_oracle()) <= spot // 10**5

    print(f"\nm{market_id}: po/ps {pool.price_oracle() / pool.price_scale():.4f}, agg {agg.price() / 1e18:.5f}")
    failures, checked = [], 0
    for k in K_GRID:
        with boa.env.anchor():
            kf = Fraction(k)
            coll = 16 * kf.numerator * amm.get_debt() * 10**18 // (9 * kf.denominator * lp_oracle.price())
            boa.env.set_storage(amm.address, COLLATERAL_SLOT, coll)
            reference, ll_price = ref.price_in_usd(lt.address), ll.price_usd()

            if _tradable(amm):
                target = probe.lp_price_at(pool.address, spot)
                if amm.get_p() > target:
                    print(f"  k={k:>6s}  AMM above the market: arbitrage would sell it LP, which we don't hold here")
                    continue
                _buy_lp(amm, target, arb)
            exit_shares = shares // 10
            with boa.env.prank(user):
                assets, stables = lt.emergency_withdraw(exit_shares)
            realised = (assets * asset_scale * spot // 10**18 + stables) * agg.price() // 10**18 * 10**18 // exit_shares

        value = max(realised, 0)                         # insolvent: the oracles floor at 0
        print(f"  k={k:>6s}  realised {realised / 1e18:12.2f}  reference {reference / 1e18:12.2f}  LL {ll_price / 1e18:12.2f}")
        for name, price in (("reference", reference), ("LL", ll_price)):
            if not value - value * SHORTFALL // 10**18 <= price <= value + value * EXCESS // 10**18:
                failures.append(f"k={k}: {name} {price} vs realised {value}")
        checked += 1
    assert not failures, "\n".join(failures)
    assert checked >= len(K_GRID) - 1
