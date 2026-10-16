"""
YBLendingOracle and YBLendingOracleLL against what an LT position actually realises.

The oracles price the LT as if the pool's spot price were at its price_oracle (price_scale may
stay shifted) and the leverage AMM were arbitraged towards the resulting LP price - as far as
AMM.exchange allows: its safe-debt limits can stop the arbitrage short, or block it entirely,
and an untradable AMM (x0 unsolvable) can't trade at all. Each scenario builds exactly that
state: move the pool, let price_oracle settle to spot, set the AMM position (collateral
overwritten, k = collateral value / (16/9 debt)), arbitrage the AMM, then exit 1% of a position
with emergency_withdraw (pro-rata, balanced pool withdrawal: marked at spot). With agg = 1, the
oracle readings taken before the arbitrage must match that exit value: tightly with a negligible
AMM fee (1e-6: an exactly-zero fee lets rounding fail the AMM's x0 check on some trade sizes);
with the real AMM fee the arbitrage stops within the fee band and the fees go to LT holders, so
the oracle reads a little below what is realised (never above).
"""
from fractions import Fraction

import boa
import pytest

EMA_TIME = 866
COLLATERAL_SLOT = 1  # AMM.collateral_amount
PRICE_ABI = '[{"name":"price","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'

# k: collateral-heavy, balanced, just inside / past the max-safe-debt limit (k = 32/17 * 9/16),
# just tradable, untradable
K_GRID = ["3", "1.25", "1.125", "1.06", "1.03", "1.001", "0.999", "0.9", "0.7"]
# pool moves: collateral sold (BTC) / crvUSD spent (millions); 50 BTC takes po/ps to ~0.53
MOVES = [0, -10, -20, -30, -40, -50, +2, +4]
# Allowed shortfall of the oracle below the realised value (it may never be above, beyond rounding)
SHORTFALL = {"tiny AMM fee": 5 * 10**14, "AMM fee": 10**16}   # 0.05% / 1%


def _tradable(amm):
    try:
        amm.value_oracle()
        return True
    except Exception:
        return False


def _arbitrage(amm, target, arb):
    """A rational arbitrageur trading the AMM towards the market LP price `target`: the largest
    trade AMM.exchange accepts while the AMM's marginal price net of its fee is still on the
    profitable side of `target`, and which makes money overall. (Past its safe-debt limit the
    AMM refuses small trades but can accept a huge loss-making one that lands on the other side
    of its curve - a donation, not arbitrage.) Returns the amount traded."""
    fee = amm.fee()
    sell = amm.get_p() > target                    # AMM above the market: sell it LP, else buy
    i, j = (1, 0) if sell else (0, 1)
    lo, hi = 0, (amm.collateral_amount() if sell else amm.get_debt())
    for _ in range(64):
        mid = (lo + hi) // 2
        with boa.env.anchor():
            try:
                with boa.env.prank(arb):
                    out = amm.exchange(i, j, mid, 0)
                p = amm.get_p()
                if sell:
                    ok = p * (10**18 - fee) >= target * 10**18 and out * 10**18 >= mid * target
                else:
                    ok = p * 10**18 <= target * (10**18 - fee) and out * target >= mid * 10**18
            except Exception:
                ok = False
        if ok:
            lo = mid
        else:
            hi = mid
    if lo > 0:
        with boa.env.prank(arb):
            amm.exchange(i, j, lo, 0)
    return lo


@pytest.mark.parametrize("amm_fee", ["tiny AMM fee", "AMM fee"])
def test_oracles_match_realised_value(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, lending_oracle, ratio_probe, mock_agg,
    ll_deployer, ll_usd_deployer, amm_fee,
):
    assert mock_agg.price() == 10**18
    if amm_fee == "tiny AMM fee":
        with boa.env.prank(admin):
            yb_lt.set_amm_fee(10**12)
    whale, trader, arb = accounts[2], accounts[3], accounts[5]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)
        cryptopool.transfer(arb, cryptopool.balanceOf(whale))      # LP to sell into the AMM
    collateral_token._mint_for_testing(admin, 10**18)
    stablecoin._mint_for_testing(admin, 10**30)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, cryptopool.price_oracle(), 0)
        yb_lt.set_rate(0)
        stablecoin.approve(yb_lt.address, 2**256 - 1)               # emergency_withdraw may need crvUSD
    collateral_token._mint_for_testing(trader, 1000 * 10**18)
    stablecoin._mint_for_testing(trader, 10 * 10**24)
    stablecoin._mint_for_testing(arb, 10**30)
    with boa.env.prank(trader):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
    with boa.env.prank(arb):
        cryptopool.approve(yb_amm.address, 2**256 - 1)
        stablecoin.approve(yb_amm.address, 2**256 - 1)
    lp_oracle = boa.loads_abi(PRICE_ABI).at(yb_amm.PRICE_ORACLE_CONTRACT())
    ll = ll_deployer.deploy()
    ll.initialize(yb_lt.address, EMA_TIME, admin)                    # unseeded: raw fundamental
    ll_usd = ll_usd_deployer.deploy(ll.address)
    for _ in range(10):
        boa.env.time_travel(1200)

    seen = {"reached": 0, "stopped": 0, "no trade": 0, "untradable": 0}
    failures = []
    print("\n move    k      po/ps   arbitrage    realised      reference   LL")
    for move in MOVES:
        for k in K_GRID:
            with boa.env.anchor():
                with boa.env.prank(trader):
                    if move < 0:
                        cryptopool.exchange(1, 0, -move * 10**18, 0)
                    elif move > 0:
                        cryptopool.exchange(0, 1, move * 10**24, 0)
                for _ in range(10):
                    boa.env.time_travel(1200)            # price_oracle settles to spot, no trades
                spot = cryptopool.last_prices()
                assert abs(spot - cryptopool.price_oracle()) <= spot // 10**6

                kf = Fraction(k)
                coll = 16 * kf.numerator * yb_amm.get_debt() * 10**18 // (9 * kf.denominator * lp_oracle.price())
                boa.env.set_storage(yb_amm.address, COLLATERAL_SLOT, coll)

                ref = lending_oracle.price_in_usd(yb_lt.address)
                ll_price = ll_usd.price()

                if _tradable(yb_amm):
                    target = ratio_probe.lp_price_at(cryptopool.address, spot)
                    traded = _arbitrage(yb_amm, target, arb)
                    # within the fee band of the market: arbitrage done
                    reached = abs(yb_amm.get_p() - target) * 10**18 <= target * (2 * yb_amm.fee() + 10**12)
                    outcome = "reached" if reached else "stopped" if traded > 0 else "no trade"
                else:
                    outcome = "untradable"
                seen[outcome] += 1

                shares = yb_lt.balanceOf(admin) // 100
                with boa.env.prank(admin):
                    assets, stables = yb_lt.emergency_withdraw(shares)
                realised = (assets * spot // 10**18 + stables) * 10**18 // shares
                po_ps = cryptopool.price_oracle() / cryptopool.price_scale()

            print(f" {move:+3d}  {k:>6s}  {po_ps:.3f}  {outcome:10s}  {realised / 1e18:10.2f}"
                  f"  {(ref / realised - 1) * 100:+8.3f}%  {(ll_price / realised - 1) * 100:+8.3f}%")
            value = max(realised, 0)                     # insolvent: the oracles floor at 0
            for name, price in (("reference", ref), ("LL", ll_price)):
                # above: spot matches price_oracle only to 1e-6
                if not value - value * SHORTFALL[amm_fee] // 10**18 <= price <= value + value // 10**6 + 10**6:
                    failures.append(f"move {move} k {k} ({outcome}): {name} {price} vs realised {realised}")

    print(seen)
    assert not failures, "\n".join(failures)
    assert all(seen.values()), f"not every arbitrage outcome covered: {seen}"
