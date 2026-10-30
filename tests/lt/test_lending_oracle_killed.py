"""
YBLendingOracle and YBLendingOracleLL on a killed market.

A killed AMM can't trade (AMM.exchange reverts), so nothing arbitrages it: its position stays
where the kill left it while the market moves on. LT.emergency_withdraw is then the only exit and
pays a pro-rata share of the whole AMM, without holding back the admin fee buffer, and a gauge
share redeems the staker's stored LT balance (a killed LT doesn't rebase the staker). Both oracles
must price exactly that exit wherever the market goes - tradable or x0-unsolvable AMM position,
solvent or not, with or without use_balances - however stale the LL's EMA. Each scenario moves the
pool, lets price_oracle settle to spot, sets the AMM position (collateral overwritten, k =
collateral value / (16/9 debt)) and kills; with agg = 1 the readings must match the realised value
of a 1% exit (marked at spot, which matches price_oracle to 1e-6), and the reference's staked
price that of a 1% gauge redemption exited the same way. Unkilling resumes the arbitrage pricing
at once: the step is the arbitrage the kill held back.
"""
from fractions import Fraction
from types import SimpleNamespace

import boa
import pytest

EMA_TIME = 866
COLLATERAL_SLOT = 1  # AMM.collateral_amount
PRICE_ABI = '[{"name":"price","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'

# k: collateral-heavy, balanced, past the max-safe-debt limit, just tradable, untradable, insolvent
K_GRID = ["3", "1.125", "1.03", "1.001", "0.999", "0.8", "0.5"]
# pool moves: collateral sold (BTC) / crvUSD spent (millions)
MOVES = [0, -20, -40, +4]
REL = 10**-5   # readings vs realised: spot matches price_oracle to 1e-6


@pytest.fixture(scope="module")
def price_abi():
    return boa.loads_abi(PRICE_ABI)


@pytest.fixture
def env(cryptopool, yb_lt, yb_amm, yb_staker, collateral_token, stablecoin, accounts, admin,
        yb_allocated, seed_cryptopool, lending_oracle, mock_agg, ll_deployer, ll_usd_deployer,
        price_abi):
    """A position half staked on a deep pool, with a positive admin fee buffer recorded by the
    LT (pool fees since the last checkpoint), a trader, and a settled LL."""
    assert mock_agg.price() == 10**18
    whale, trader = accounts[2], accounts[3]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)
    collateral_token._mint_for_testing(admin, 10**18)
    stablecoin._mint_for_testing(admin, 10**30)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, cryptopool.price_oracle(), 0)
        yb_lt.set_rate(0)
        yb_staker.deposit(yb_lt.balanceOf(admin) // 2, admin)
        stablecoin.approve(yb_lt.address, 2**256 - 1)               # emergency_withdraw may need crvUSD
    collateral_token._mint_for_testing(trader, 1000 * 10**18)
    stablecoin._mint_for_testing(trader, 10 * 10**24)
    with boa.env.prank(trader):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        for _ in range(10):                                         # pool fees: the LT gains value
            got = cryptopool.exchange(0, 1, 2 * 10**24, 0)
            cryptopool.exchange(1, 0, got, 0)
    for _ in range(10):
        boa.env.time_travel(1200)
    with boa.env.prank(admin):
        yb_staker.deposit(10**12, admin)                            # checkpoint: records the admin buffer
    ll = ll_deployer.deploy()
    ll.initialize(yb_lt.address, EMA_TIME, admin)
    for _ in range(12):
        ll.price_w()
        boa.env.time_travel(600)
    e = SimpleNamespace(pool=cryptopool, lt=yb_lt, amm=yb_amm, staker=yb_staker, ref=lending_oracle,
                        ll=ll, ll_usd=ll_usd_deployer.deploy(ll.address), admin=admin, trader=trader,
                        lp_oracle=price_abi.at(yb_amm.PRICE_ORACLE_CONTRACT()))
    liq = yb_lt.liquidity()
    assert liq[0] > liq[1] // 10**3, "admin fee buffer too small to tell apart"
    return e


def _tradable(amm):
    try:
        amm.value_oracle()
        return True
    except Exception:
        return False


def _settle(e):
    """price_oracle settles to spot with no trades (see test_lending_oracle_realised)."""
    with boa.env.prank(e.trader):
        for _ in range(2):
            e.pool.exchange(0, 1, 10**15, 0)
    for _ in range(10):
        boa.env.time_travel(1200)
    spot = e.pool.last_prices()
    assert abs(spot - e.pool.price_oracle()) <= spot // 10**6


def _move(e, move):
    with boa.env.prank(e.trader):
        if move < 0:
            e.pool.exchange(1, 0, -move * 10**18, 0)
        elif move > 0:
            e.pool.exchange(0, 1, move * 10**24, 0)


def _set_k(e, k):
    kf = Fraction(k)
    coll = 16 * kf.numerator * e.amm.get_debt() * 10**18 // (9 * kf.denominator * e.lp_oracle.price())
    boa.env.set_storage(e.amm.address, COLLATERAL_SLOT, coll)


def _kill(e, killed=True):
    with boa.env.prank(e.admin):
        e.lt.set_killed(killed)
    assert e.amm.is_killed() == killed


def _readings(e):
    lt = e.lt.address
    return dict(ref=e.ref.price_in_usd(lt), ref_bal=e.ref.price_in_usd(lt, True),
                ref_asset=e.ref.price_in_asset(lt), ll=e.ll_usd.price(), ll_asset=e.ll.price(),
                staked=e.ref.staked_price_in_usd(lt), staked_bal=e.ref.staked_price_in_usd(lt, True))


def _exit(e, shares):
    """emergency_withdraw `shares` LT of the admin: realised USD value per share (agg = 1)."""
    spot = e.pool.last_prices()
    with boa.env.prank(e.admin):
        assets, stables = e.lt.emergency_withdraw(shares)
    return (assets * spot // 10**18 + stables) * 10**18 // shares


def _close(price, realised):
    value = max(realised, 0)                             # insolvent: the oracles floor at 0
    return abs(price - value) <= value * REL + 10**6


def test_killed_prices_match_exit(env):
    e = env
    seen = {"tradable": 0, "untradable": 0, "insolvent": 0}
    failures = []
    for move in MOVES:
        with boa.env.anchor():
            _move(e, move)
            _settle(e)
            for k in K_GRID:
                with boa.env.anchor():
                    _set_k(e, k)
                    tradable = _tradable(e.amm)
                    live = _readings(e)
                    _kill(e)
                    r = _readings(e)
                    ctx = f"move {move} k {k}"

                    # gauge share -> LT at the stored staker balance: redeem 1% of the admin's
                    # gauge shares (ERC4626 rounding: the oracle's (balance + 1) / (supply + 1))
                    g_shares = e.staker.balanceOf(e.admin) // 100
                    lt_before = e.lt.balanceOf(e.admin)
                    scale = (e.lt.balanceOf(e.staker.address) + 1) * 10**18 // (e.staker.totalSupply() + 1)
                    with boa.env.prank(e.admin):
                        e.staker.redeem(g_shares, e.admin, e.admin)
                    redeemed = e.lt.balanceOf(e.admin) - lt_before
                    assert abs(redeemed - g_shares * scale // 10**18) <= 1, ctx

                    realised = _exit(e, e.lt.balanceOf(e.admin) // 100)
                    realised_staked = realised * redeemed // g_shares
                    outcome = "insolvent" if realised <= 0 else "tradable" if tradable else "untradable"
                    seen[outcome] += 1
                    print(f"{ctx} ({outcome}): killed {r['ref'] / max(realised, 1) - 1:+.2e} vs exit, "
                          f"live {live['ref'] / max(realised, 1) - 1:+.2e}")

                    for name in ("ref", "ref_bal", "ll"):
                        if not _close(r[name], realised):
                            failures.append(f"{ctx} ({outcome}): {name} {r[name]} vs realised {realised}")
                    for name in ("staked", "staked_bal"):
                        if not _close(r[name], realised_staked):
                            failures.append(f"{ctx} ({outcome}): {name} {r[name]} vs realised {realised_staked}")
                    assert r["ref"] == r["ref_bal"], ctx
                    assert r["staked"] == r["staked_bal"], ctx
                    assert abs(r["ll"] - r["ref"]) <= r["ref"] // 10**12 + 10, ctx
                    assert abs(r["ll_asset"] - r["ref_asset"]) <= r["ref_asset"] // 10**12 + 10, ctx
                    if not tradable:
                        # was already the balance value, less the admin fee buffer
                        assert live["ref"] <= r["ref"], ctx
    print(seen)
    assert not failures, "\n".join(failures)
    assert all(seen.values()), f"not every position covered: {seen}"


def test_unkill_resumes_arbitrage_pricing(env):
    """Killed for a day while the market falls: the AMM doesn't follow, so the price is the
    balance value, above the arbitrage value. Checkpoints keep the LL's EMA on the fundamental,
    so on unkill both oracles drop together to the arbitrage value less the admin fee buffer
    (what the first trade leaves, test_lending_oracle_realised), and back up on a re-kill."""
    e = env
    before = _readings(e)
    _kill(e)
    killed = _readings(e)
    # up by the admin fee buffer (now paid out) and the arbitrage pending since the pool fees
    assert killed["ref"] > before["ref"] and killed["ll"] > before["ll"]

    _move(e, -10)
    for _ in range(144):
        boa.env.time_travel(600)
        e.ll.price_w()
    _settle(e)                                            # its trades move price_scale
    for _ in range(2):                                    # the EMA blends the previous sample
        e.ll.price_w()
        boa.env.time_travel(20 * EMA_TIME)
    assert _tradable(e.amm)
    k = _readings(e)
    _approx = lambda a, b, rel: abs(a - b) <= b * rel   # noqa: E731
    assert _approx(k["ll"], k["ref"], 10**-12)

    _kill(e, False)
    u = _readings(e)
    print(f"\nunkill: ref {k['ref']} -> {u['ref']} ({(u['ref'] - k['ref']) / k['ref'] * 100:+.3f}%)")
    assert u["ref"] < k["ref"] * 999 // 1000              # admin buffer + held-back arbitrage
    assert u["ref"] < u["ref_bal"]                         # live: arbitrage value below balances
    assert _approx(u["ll"], u["ref"], 10**-6)             # the EMA followed the fundamental

    _kill(e)
    assert _readings(e) == k
