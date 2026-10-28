"""
YBLendingOracle and YBLendingOracleLL during pool A ramps.

The pool caches D and virtual_price at its last interaction, solved with the A of that time;
during a ramp its next trade re-solves D at the current A (Twocrypto._get_D). The oracles do the
same while the ramp hasn't been observed by a trade (future_A_gamma_time > last_timestamp), so a
reading on an idle, ramping pool must equal the reading right after a dust trade refreshes it -
mid-ramp, after the ramp has ended, and after stop_ramp_A_gamma (where pairing the current A with
the stale D was the failure mode). The pool sits ~10% off price_scale, where the LP value depends
on A, with price_scale frozen (adjustment steps 0) so the refreshing trade can't also rebalance it.
"""
from types import SimpleNamespace

import boa
import pytest

EMA_TIME = 866
RAMP_TIME = 86400
KEEP = 2**255  # apply_new_parameters: out-of-range values keep the current fee / ma_time


@pytest.fixture
def env(cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
        yb_allocated, seed_cryptopool, lending_oracle, ll_deployer, ll_usd_deployer):
    whale, trader = accounts[2], accounts[3]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, cryptopool.price_oracle(), 0)
        cryptopool.apply_new_parameters(KEEP, KEEP, KEEP, 0, 0, KEEP)    # freeze price_scale
    collateral_token._mint_for_testing(trader, 1000 * 10**18)
    stablecoin._mint_for_testing(trader, 10**24)
    with boa.env.prank(trader):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        cryptopool.exchange(1, 0, 20 * 10**18, 0)                         # ~10% off price_scale
    for _ in range(10):                                                   # price_oracle -> spot
        boa.env.time_travel(1200)
    _dust(SimpleNamespace(pool=cryptopool, trader=trader))
    boa.env.time_travel(1200)

    ll = ll_deployer.deploy()
    ll.initialize(yb_lt.address, EMA_TIME, admin)                         # unseeded: raw fundamental
    e = SimpleNamespace(pool=cryptopool, lt=yb_lt, ref=lending_oracle, ll=ll,
                        ll_usd=ll_usd_deployer.deploy(ll.address), admin=admin, trader=trader)
    assert abs(cryptopool.price_oracle() * 100 // cryptopool.price_scale() - 100) >= 5
    return e


def _dust(e):
    with boa.env.prank(e.trader):
        e.pool.exchange(0, 1, 10**15, 0)


def _readings(e):
    return (e.ref.price_in_asset(e.lt.address), e.ref.price_in_usd(e.lt.address),
            e.ll.price(), e.ll_usd.price())


def _assert_same(a, b, rel=10**-8):
    for x, y in zip(a, b):
        assert abs(x - y) <= max(2, y * rel), f"{a} != {b}"


def _ramp(e, mult):
    with boa.env.prank(e.admin):
        e.pool.ramp_A_gamma(int(e.pool.A() * mult), e.pool.gamma(), boa.env.evm.patch.timestamp + RAMP_TIME)


def _refresh(e):
    """Dust-trade the pool; return (readings after, in the same block, and D's relative move)."""
    D, ps = e.pool.D(), e.pool.price_scale()
    _dust(e)
    assert e.pool.price_scale() == ps
    return _readings(e), abs(e.pool.D() - D) / D


@pytest.mark.parametrize("mult", [10, 2, 0.5, 0.1])
@pytest.mark.parametrize("idle", [RAMP_TIME // 4, 2 * RAMP_TIME])
def test_idle_ramp_matches_refresh(env, mult, idle):
    """Mid-ramp (6 h into a day) and after the ramp has ended without a trade observing it:
    the reading on the stale cache equals the reading once a trade re-solves D."""
    _ramp(env, mult)
    boa.env.time_travel(idle)
    before = _readings(env)
    after, d_move = _refresh(env)
    assert d_move > 10**-5                     # the cached D really was stale
    _assert_same(before, after)


@pytest.mark.parametrize("mult", [10, 0.1])
def test_stop_ramp_while_idle(env, mult):
    """stop_ramp_A_gamma on an idle pool (YB-05): the stop itself doesn't move the reading, and
    an hour later (A stays at the stopped value) it matches the refreshed pool."""
    _ramp(env, mult)
    boa.env.time_travel(RAMP_TIME // 2)
    before = _readings(env)
    with boa.env.prank(env.admin):
        env.pool.stop_ramp_A_gamma()
    assert _readings(env) == before
    boa.env.time_travel(3600)
    before = _readings(env)
    after, d_move = _refresh(env)
    assert d_move > 10**-5
    _assert_same(before, after)


def test_same_block_trade_uses_cached_D(env, cryptopool):
    """The premise for skipping the re-solve when the pool traded in this block: D is then
    already solved at the current A, i.e. the re-solve would reproduce the cached D."""
    # Named inputs: boa reuses this ABI to format any later call trace through newton_D.
    math = boa.loads_abi("""[{"name":"newton_D","stateMutability":"view","type":"function","outputs":[{"name":"","type":"uint256"}],
        "inputs":[{"name":"ANN","type":"uint256"},{"name":"gamma","type":"uint256"},
                  {"name":"x_unsorted","type":"uint256[2]"},{"name":"K0_prev","type":"uint256"}]}]""").at(cryptopool.MATH())
    _ramp(env, 10)
    boa.env.time_travel(RAMP_TIME // 4)
    _dust(env)
    pool = env.pool
    prec = pool.precisions()
    xp = [pool.balances(0) * prec[0], pool.balances(1) * prec[1] * pool.price_scale() // 10**18]
    assert pool.last_timestamp() == boa.env.evm.patch.timestamp < pool.future_A_gamma_time()
    assert math.newton_D(pool.A(), pool.gamma(), xp, 0) == pool.D()


def test_no_ramp_idle_matches_refresh(env):
    """No ramp: the cached D is exact, idle or not."""
    boa.env.time_travel(RAMP_TIME)
    before = _readings(env)
    after, d_move = _refresh(env)
    assert d_move < 10**-9
    _assert_same(before, after)
