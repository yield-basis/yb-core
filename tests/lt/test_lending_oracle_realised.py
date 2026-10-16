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
import os
from fractions import Fraction
from math import isqrt
from types import SimpleNamespace

import boa
import pytest
from hypothesis import HealthCheck, assume, given, settings, strategies as st

FUZZ_EXAMPLES = int(os.getenv("REALISED_FUZZ_EXAMPLES", "50"))
# Independent fuzz runs, for a long campaign on all cores (PyPy: one per physical core), e.g.
#   FUZZ_SHARDS=4 REALISED_FUZZ_EXAMPLES=2000 uv run pytest -n 4 tests/lt/test_lending_oracle_realised.py -k fuzz
FUZZ_SHARDS = int(os.getenv("FUZZ_SHARDS", "1"))
EMA_TIME = 866
COLLATERAL_SLOT = 1  # AMM.collateral_amount
PRICE_ABI = '[{"name":"price","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'

# k: collateral-heavy, balanced, past the max-safe-debt limit (k = 32/17 * 9/16 = 1.0588),
# just tradable, untradable
K_GRID = ["3", "1.125", "1.03", "1.001", "0.999", "0.8"]
# pool moves: collateral sold (BTC) / crvUSD spent (millions); 40 / 50 BTC take po/ps to ~0.78 /
# ~0.53, where arbitrage stops at the AMM's limit
MOVES = [0, -20, -40, -50, +4]
TINY_FEE = 10**12
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
    c = amm.collateral_amount()
    p0 = amm.get_p()
    x_i = p0 * c // 10**18                         # x0 - debt
    sell = p0 > target                             # AMM above the market: sell it LP, else buy
    i, j = (1, 0) if sell else (0, 1)
    goal = target * 10**18 // (10**18 - fee) if sell else target * (10**18 - fee) // 10**18

    def trial(amount):
        """(accepted, AMM price after, profitable)"""
        with boa.env.anchor():
            try:
                with boa.env.prank(arb):
                    out = amm.exchange(i, j, amount, 0)
            except Exception:
                return False, 0, False
            p = amm.get_p()
        return True, p, (out * 10**18 >= amount * target) if sell else (out * target >= amount * 10**18)

    def good(amount):
        accepted, p, profitable = trial(amount)
        return accepted and profitable and (p >= goal if sell else p <= goal)

    # Trade along the AMM curve x * y = x_i * c to the marginal price `goal`, corrected for the
    # fee the AMM keeps by a few Newton steps on the actual price after the trade.
    first = isqrt(x_i * c * 10**18 // goal) - c if sell else isqrt(goal * x_i * c // 10**18) - x_i
    amount = max(first, 0)
    for _ in range(4):
        amount = amount * (10**9 - 1) // 10**9   # a hair short: rounding stays on the right side
        accepted, p, profitable = trial(amount) if amount > 0 else (False, 0, False)
        if not accepted:
            break
        if profitable and (p >= goal if sell else p <= goal):
            with boa.env.prank(arb):
                amm.exchange(i, j, amount, 0)
            return amount
        if sell:
            amount = int((c + amount) * (p / goal) ** 0.5) - c
        else:
            amount = int((x_i + amount) * (goal / p) ** 0.5) - x_i

    # The AMM refuses it: its safe-debt limits stop the arbitrage short, or block it entirely.
    hi = max(first, 0)
    lo = hi // 10**6
    if hi == 0 or not good(lo):
        return 0
    while hi - lo > max(hi // 10**5, 1):
        mid = (lo + hi) // 2
        if good(mid):
            lo = mid
        else:
            hi = mid
    with boa.env.prank(arb):
        amm.exchange(i, j, lo, 0)
    return lo


@pytest.fixture(scope="module")
def price_abi():
    return boa.loads_abi(PRICE_ABI)


@pytest.fixture
def env(cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
        yb_allocated, seed_cryptopool, lending_oracle, ratio_probe, mock_agg,
        ll_deployer, ll_usd_deployer, price_abi):
    """A position on a deep pool, a trader, an arbitrageur holding LP and crvUSD, and an
    unseeded LL (raw fundamental, directly comparable)."""
    assert mock_agg.price() == 10**18
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
    ll = ll_deployer.deploy()
    ll.initialize(yb_lt.address, EMA_TIME, admin)
    for _ in range(10):
        boa.env.time_travel(1200)
    return SimpleNamespace(
        pool=cryptopool, lt=yb_lt, amm=yb_amm, ref=lending_oracle, probe=ratio_probe, admin=admin,
        trader=trader, arb=arb, ll_usd=ll_usd_deployer.deploy(ll.address),
        lp_oracle=price_abi.at(yb_amm.PRICE_ORACLE_CONTRACT()))


def _settle(env):
    """price_oracle settles to spot with no trades. Two dust trades in one block first: a trade
    can move price_scale (the first one in a block) after recording last_prices, which then isn't
    the pool's actual spot; the second can't. False if price_oracle can't reach spot (past its
    clamp)."""
    with boa.env.prank(env.trader):
        for _ in range(2):
            env.pool.exchange(0, 1, 10**15, 0)
    for _ in range(10):
        boa.env.time_travel(1200)
    spot = env.pool.last_prices()
    return abs(spot - env.pool.price_oracle()) <= spot // 10**6


def _check(env, k, amm_fee):
    """Set the AMM position to k, read both oracles, arbitrage, exit 1%. Returns (outcome,
    realised, failures). Leaves the state changed: call inside an anchor."""
    kf = Fraction(k)
    coll = 16 * kf.numerator * env.amm.get_debt() * 10**18 // (9 * kf.denominator * env.lp_oracle.price())
    boa.env.set_storage(env.amm.address, COLLATERAL_SLOT, coll)
    ref, ll_price = env.ref.price_in_usd(env.lt.address), env.ll_usd.price()

    spot = env.pool.last_prices()
    if _tradable(env.amm):
        target = env.probe.lp_price_at(env.pool.address, spot)
        traded = _arbitrage(env.amm, target, env.arb)
        # within the fee band of the market: arbitrage done
        reached = abs(env.amm.get_p() - target) * 10**18 <= target * (2 * env.amm.fee() + 10**12)
        outcome = "reached" if reached else "stopped" if traded > 0 else "no trade"
    else:
        outcome = "untradable"

    shares = env.lt.balanceOf(env.admin) // 100
    with boa.env.prank(env.admin):
        assets, stables = env.lt.emergency_withdraw(shares)
    realised = (assets * spot // 10**18 + stables) * 10**18 // shares

    value = max(realised, 0)                             # insolvent: the oracles floor at 0
    failures = []
    for name, price in (("reference", ref), ("LL", ll_price)):
        # above: spot matches price_oracle only to 1e-6
        if not value - value * SHORTFALL[amm_fee] // 10**18 <= price <= value + value // 10**6 + 10**6:
            failures.append(f"k {k} ({outcome}): {name} {price} vs realised {realised}")
    return outcome, realised, failures


def _move(env, move):
    with boa.env.prank(env.trader):
        if move < 0:
            env.pool.exchange(1, 0, -move * 10**18, 0)
        elif move > 0:
            env.pool.exchange(0, 1, move * 10**24, 0)


def test_oracles_match_realised_value(env):
    """The grid, with a negligible AMM fee: a tight match (the fuzz covers the real fee too)."""
    amm_fee = "tiny AMM fee"
    with boa.env.prank(env.admin):
        env.lt.set_amm_fee(TINY_FEE)
    seen = {"reached": 0, "stopped": 0, "no trade": 0, "untradable": 0}
    failures = []
    for move in MOVES:
        with boa.env.anchor():
            _move(env, move)
            assert _settle(env)
            for k in K_GRID:
                with boa.env.anchor():
                    outcome, realised, fails = _check(env, k, amm_fee)
                seen[outcome] += 1
                failures += [f"move {move} " + f for f in fails]
    print(seen)
    assert not failures, "\n".join(failures)
    assert all(seen.values()), f"not every arbitrage outcome covered: {seen}"


@pytest.mark.parametrize("shard", range(FUZZ_SHARDS))
@given(
    ops=st.lists(st.tuples(st.integers(0, 3), st.integers(1, 100)), min_size=1, max_size=6),
    k_permille=st.integers(600, 3000),
    tiny_fee=st.booleans(),
)
@settings(max_examples=FUZZ_EXAMPLES, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
def test_oracles_match_realised_value_fuzz(env, shard, ops, k_permille, tiny_fee):
    """Random pool trades with time in between (so price_scale moves the way it does), then a
    random AMM position: the oracles must match the realised value as in the grid test."""
    amm_fee = "tiny AMM fee" if tiny_fee else "AMM fee"
    if tiny_fee:
        with boa.env.prank(env.admin):
            env.lt.set_amm_fee(TINY_FEE)
    for op, param in ops:
        try:
            if op == 0:                                    # sell collateral: price down
                _move(env, -max(1, param // 2))
            elif op == 1:                                  # buy collateral: price up
                with boa.env.prank(env.trader):
                    env.pool.exchange(0, 1, param * 20_000 * 10**18, 0)
            elif op == 2:                                  # wash: fees, price_scale tweaks
                with boa.env.prank(env.trader):
                    got = env.pool.exchange(0, 1, param * 10_000 * 10**18, 0)
                    env.pool.exchange(1, 0, got, 0)
        except Exception:
            pass                                           # over-sized for the pool: skip
        boa.env.time_travel(param * 60)
    assume(_settle(env))
    _, _, failures = _check(env, Fraction(k_permille, 1000), amm_fee)
    assert not failures, "\n".join(failures)
