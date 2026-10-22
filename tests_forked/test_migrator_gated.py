import boa
import pytest
from tests_forked.networks import NETWORK

# LTMigratorGated: WETH m6 -> m10 once Curve vote 1508 attaches YBOraclizedPolicy
# to m10's cryptopool. Its LT gate admits a deposit only at the LevAMM book's
# debt / collateral ratio, which LTMigrator's price_oracle debt misses.
# The book's ratio suits any market, so every forward migration is tested too.

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
NEW_POLICY = "0xAEa95d551154443E73565bE724d59D63d72f8A7d"  # set by Curve vote 1508
NEW_RPF = 43 * 10**10 // 100
FROM, TO = 6, 10
FORWARD = [(0, 7), (1, 8), (2, 9), (3, 7), (4, 8), (5, 9), (6, 10)]  # WBTC, cbBTC, tBTC: v1, v2 -> v3; WETH v2 -> v3
VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
# Pending proposal: LTMigrator 0xd5b450... -> 0xEacb6032... (contracts/LTMigrator.vy)
SWAP_PROPOSAL = 68833251590147929654715939790700980093137965976260969226758254139376820549194
LT_MIGRATOR = "0xEacb6032d9d76D87DbD95bF05E2210820767cB0F"
SUPERSEDED_LT_MIGRATOR = "0xd5b450fDD5aF5341436Fc37A46BBEc39CA8F92CB"
CRVUSD_SOURCE_MARKET = 7  # WBTC AMM: idle crvUSD for the attacker, unrelated to both pools
PREVIEW_PRECISION = 10**9  # preview vs result: rounding only

ERC20_ABI = """[
 {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"to"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"},
 {"name":"deposit","outputs":[],"inputs":[],"stateMutability":"payable","type":"function"}
]"""
GAUGE_ABI = """[
 {"name":"deposit","outputs":[{"type":"uint256"}],"inputs":[{"type":"uint256","name":"assets"},{"type":"address","name":"receiver"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"}
]"""
ADMIN_ABI = '[{"name":"admin","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"}]'
PRICE_ABI = '[{"name":"price","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier="latest"):
        yield


@pytest.fixture(scope="module")
def factory():
    return boa.load_partial("contracts/Factory.vy").at(FACTORY)


@pytest.fixture(scope="module")
def owner(factory):
    return boa.load_partial("contracts/HybridFactoryOwner.vy").at(factory.admin())


def _pair(factory, i, j):
    lt = boa.load_partial("contracts/LT.vy")
    m_from, m_to = factory.markets(i), factory.markets(j)
    return dict(
        lt_from=lt.at(m_from.lt), gauge_from=boa.loads_abi(GAUGE_ABI).at(m_from.staker),
        lt_to=lt.at(m_to.lt), gauge_to=boa.loads_abi(GAUGE_ABI).at(m_to.staker),
        amm=boa.load_partial("contracts/AMM.vy").at(m_to.amm),
        pool=boa.load_partial("contracts/twocrypto_pool/contracts/main/Twocrypto.vy").at(m_to.cryptopool),
    )


@pytest.fixture(scope="module")
def market(factory):
    return _pair(factory, FROM, TO)


@pytest.fixture(scope="module", autouse=True)
def policy(market):
    """Curve vote 1508, unless it has executed already"""
    pool = market["pool"]
    if pool.POLICY() != NEW_POLICY:
        with boa.env.prank(boa.loads_abi(ADMIN_ABI).at(pool.factory()).admin()):
            pool.set_policy_contract(NEW_POLICY)
            pool.set_fee_parameters(NEW_RPF, 0)
    assert pool.POLICY() == NEW_POLICY


@pytest.fixture(scope="module", autouse=True)
def migrator_swap(owner):
    """The pending LTMigrator swap, unless it has executed: its actions, as the DAO"""
    if not owner.limit_setters(LT_MIGRATOR):
        voting = boa.load_abi("scripts/voting/TokenVoting.abi.json", name="AragonVoting").at(VOTING_PLUGIN)
        with boa.env.prank(owner.ADMIN()):
            for to, value, data in voting.getProposal(SWAP_PROPOSAL)[4]:
                boa.env.raw_call(to_address=to, value=value, data=data)
    assert owner.limit_setters(LT_MIGRATOR)
    assert not owner.limit_setters(SUPERSEDED_LT_MIGRATOR)


@pytest.fixture(scope="module")
def migrator(owner):
    migrator = boa.load("contracts/utils/LTMigratorGated.vy", CRVUSD, owner.address)
    with boa.env.prank(owner.ADMIN()):
        owner.set_limit_setter(migrator.address, True)
    return migrator


@pytest.fixture(scope="module")
def old_migrator():
    return boa.load_partial("contracts/LTMigrator.vy").at(LT_MIGRATOR)


@pytest.fixture(scope="module")
def weth():
    return boa.loads_abi(ERC20_ABI).at(WETH)


@pytest.fixture(scope="module")
def crvusd():
    return boa.loads_abi(ERC20_ABI).at(CRVUSD)


def _fund(market, fraction, staked):
    """Give a fresh user `fraction` of lt_from staked in its gauge (as LT, or as gauge shares)"""
    lt, gauge = market["lt_from"], market["gauge_from"]
    user = boa.env.generate_address()
    amount = int(lt.balanceOf(gauge.address) * fraction)
    with boa.env.prank(gauge.address):
        lt.transfer(user, amount)
    if staked:
        with boa.env.prank(user):
            lt.approve(gauge.address, 2**256 - 1)
            amount = gauge.deposit(amount, user)
    return user, amount


def _migrate(migrator, market, user, amount, staked, min_out=0, *args):
    lt_from, lt_to = market["lt_from"].address, market["lt_to"].address
    with boa.env.prank(user):
        if staked:
            market["gauge_from"].approve(migrator.address, 2**256 - 1)
            migrator.migrate_staked(lt_from, lt_to, amount, min_out, *args)
        else:
            market["lt_from"].approve(migrator.address, 2**256 - 1)
            migrator.migrate_plain(lt_from, lt_to, amount, min_out, *args)
    return (market["gauge_to"] if staked else market["lt_to"]).balanceOf(user)


def _preview(migrator, market, amount, staked):
    preview = migrator.preview_migrate_staked if staked else migrator.preview_migrate_plain
    return preview(market["lt_from"].address, market["lt_to"].address, amount)


def _book(market):
    return market["amm"].get_debt(), market["amm"].collateral_amount()


def test_old_migrator_fails_gate(old_migrator, market):
    user, amount = _fund(market, 0.5, False)
    with boa.reverts("LT deviation"):
        _migrate(old_migrator, market, user, amount, False)


def _check_migration(migrator, market, fraction, staked):
    lt_to = market["lt_to"]
    user, amount = _fund(market, fraction, staked)
    allocation = lt_to.stablecoin_allocation()
    preview = _preview(migrator, market, amount, staked)
    debt0, collateral0 = _book(market)

    received = _migrate(migrator, market, user, amount, staked, preview - preview // PREVIEW_PRECISION)

    source = market["gauge_from"] if staked else market["lt_from"]
    assert source.balanceOf(user) == 0
    assert received > 0
    assert abs(received - preview) <= preview // PREVIEW_PRECISION
    assert lt_to.stablecoin_allocation() == allocation
    # Deposited at the book's ratio, far inside the gate's 0.01%
    debt1, collateral1 = _book(market)
    proportional = (collateral1 - collateral0) * debt0 // collateral0
    assert abs((debt1 - debt0) - proportional) <= proportional // 10**6


@pytest.mark.parametrize("staked", [False, True])
@pytest.mark.parametrize("fraction", [0.001, 1.0])
def test_migrate(migrator, market, fraction, staked):
    _check_migration(migrator, market, fraction, staked)


@pytest.mark.parametrize("staked", [False, True])
@pytest.mark.parametrize("i,j", FORWARD)
def test_migrate_forward(migrator, factory, i, j, staked):
    """Every forward migration, into gated (m10) and ungated markets"""
    _check_migration(migrator, _pair(factory, i, j), 0.01, staked)


@pytest.mark.parametrize("sell_weth", [True, False])
def test_migrate_skewed_pool(migrator, market, weth, crvusd, sell_weth):
    """The solver converges with the pool far off balance and price_scale lagging"""
    pool = market["pool"]
    trader = boa.env.generate_address()
    if sell_weth:
        boa.env.set_balance(trader, 1201 * 10**18)
        with boa.env.prank(trader):
            weth.deposit(value=1200 * 10**18)
            weth.approve(pool.address, 2**256 - 1)
            pool.exchange(1, 0, 1200 * 10**18, 0)
    else:
        source = boa.load_partial("contracts/Factory.vy").at(FACTORY).markets(CRVUSD_SOURCE_MARKET).amm
        with boa.env.prank(source):
            crvusd.transfer(trader, 3 * 10**6 * 10**18)
        with boa.env.prank(trader):
            crvusd.approve(pool.address, 2**256 - 1)
            pool.exchange(0, 1, 3 * 10**6 * 10**18, 0)
    boa.env.time_travel(seconds=3600)

    user, amount = _fund(market, 1.0, False)
    preview = _preview(migrator, market, amount, False)
    debt0, collateral0 = _book(market)
    received = _migrate(migrator, market, user, amount, False)
    assert abs(received - preview) <= preview // PREVIEW_PRECISION
    debt1, collateral1 = _book(market)
    proportional = (collateral1 - collateral0) * debt0 // collateral0
    assert abs((debt1 - debt0) - proportional) <= proportional // 10**6


def test_debt_coefficient_ignored(migrator, market):
    user, amount = _fund(market, 0.5, False)
    with boa.env.anchor():
        default = _migrate(migrator, market, user, amount, False)
    assert _migrate(migrator, market, user, amount, False, 0, 9 * 10**17) == default


@pytest.mark.parametrize("i,j,reason", [(TO, TO, "same LT"), (3, TO, "lt_to superseded"), (FROM, 3, "lt_to deprecated")])
def test_migrate_rejected(migrator, factory, i, j, reason):
    lt_from, lt_to = factory.markets(i).lt, factory.markets(j).lt
    with boa.reverts(reason):
        migrator.preview_migrate_plain(lt_from, lt_to, 10**18)
    with boa.reverts(reason):
        migrator.migrate_plain(lt_from, lt_to, 0, 0)


@pytest.fixture(scope="module")
def attacker(factory, market, weth, crvusd):
    """Holds WETH, crvUSD and m10 LP, approved to the pool and the LevAMM"""
    pool, amm, lt_to = market["pool"], market["amm"], market["lt_to"]
    attacker = boa.env.generate_address()
    boa.env.set_balance(attacker, 20_001 * 10**18)
    with boa.env.prank(attacker):
        weth.deposit(value=20_000 * 10**18)
    with boa.env.prank(factory.markets(CRVUSD_SOURCE_MARKET).amm):
        crvusd.transfer(attacker, 5 * 10**6 * 10**18)
    # LP allowlist: only lt_to may add liquidity
    with boa.env.prank(attacker):
        weth.transfer(lt_to.address, 2_000 * 10**18)
    with boa.env.prank(lt_to.address):
        weth.approve(pool.address, 2**256 - 1)
        pool.add_liquidity([0, 2_000 * 10**18], 0, attacker)
    with boa.env.prank(attacker):
        for spender in (pool.address, amm.address):
            weth.approve(spender, 2**256 - 1)
            crvusd.approve(spender, 2**256 - 1)
        pool.approve(amm.address, 2**256 - 1)
    return attacker


@pytest.mark.parametrize("venue", ["pool", "amm"])
@pytest.mark.parametrize("sell_stable", [False, True])
@pytest.mark.parametrize("fraction", [0.002, 0.01, 0.03])
def test_destination_sandwich_unprofitable(migrator, market, attacker, venue, sell_stable, fraction):
    """
    Front-run lt_to's cryptopool or LevAMM, let the whole of lt_from's gauge
    migrate, trade the proceeds back. In the pool, the gated deposit trades
    against the attacker. In the LevAMM, the deposit is proportional and the
    attacker gains from convexity, but far less than the LevAMM fee.
    """
    pool, amm = market["pool"], market["amm"]
    target = pool if venue == "pool" else amm
    i = 0 if sell_stable else 1
    if venue == "pool":
        price = pool.price_oracle()                     # WETH in crvUSD
        size = pool.balances(1) * fraction              # WETH
    else:
        price = boa.loads_abi(PRICE_ABI).at(amm.PRICE_ORACLE_CONTRACT()).price()  # LP in crvUSD
        size = amm.collateral_amount() * fraction       # LP
    dx = int(size * price / 10**18) if sell_stable else int(size)
    user, amount = _fund(market, 1.0, False)

    with boa.env.prank(attacker):
        out = target.exchange(i, 1 - i, dx, 0)
    _migrate(migrator, market, user, amount, False)
    with boa.env.prank(attacker):
        back = target.exchange(1 - i, i, out, 0)

    assert back < dx
