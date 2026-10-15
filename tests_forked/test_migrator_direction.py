import boa
import pytest
from eth_utils import keccak
from tests_forked.networks import NETWORK

# LTMigrator direction rule: lt_to must be the newest market for lt_from's
# asset. Sources can be live (v1 m0..m2) or disabled (v2 m3..m6).
#
# Markets: WBTC m0 / m3 / m7, cbBTC m1 / m4 / m8, tBTC m2 / m5 / m9, WETH m6 / m10.

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
FACTORY_OWNER = "0xb8BA33CD1Ccb091a8468572950bD3669723FA5C6"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
DISABLED_LTS_SLOT = 0  # HybridFactoryOwner storage layout

FORWARD = [(0, 7), (1, 8), (2, 9), (3, 7), (4, 8), (5, 9), (6, 10)]
REJECTED = [
    (7, 7, "same LT"),
    (0, 3, "lt_to deprecated"),     # v1 -> v2: v2 is disabled
    (7, 3, "lt_to deprecated"),     # backwards into a disabled market
    (7, 0, "lt_to superseded"),     # backwards into a live v1 market
    (3, 0, "lt_to superseded"),
    (3, 10, "lt_to superseded"),    # different asset
]

GAUGE_ABI = """[
 {"name":"deposit","outputs":[{"type":"uint256"}],"inputs":[{"type":"uint256","name":"assets"},{"type":"address","name":"receiver"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"}
]"""


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier="latest"):
        yield


@pytest.fixture(scope="module")
def factory():
    return boa.load_partial("contracts/Factory.vy").at(FACTORY)


@pytest.fixture(scope="module")
def owner():
    return boa.load_partial("contracts/HybridFactoryOwner.vy").at(FACTORY_OWNER)


@pytest.fixture(scope="module")
def lt_deployer():
    return boa.load_partial("contracts/LT.vy")


@pytest.fixture(scope="module")
def markets(factory, lt_deployer):
    return [(lt_deployer.at(m[3]), boa.loads_abi(GAUGE_ABI).at(m[6])) for m in
            (factory.markets(i) for i in range(factory.market_count()))]


@pytest.fixture(scope="module")
def migrator(owner):
    migrator = boa.load("contracts/LTMigrator.vy", CRVUSD, owner.address)
    with boa.env.prank(owner.ADMIN()):
        owner.set_limit_setter(migrator.address, True)
    return migrator


def _fund(markets, i, staked):
    """Give a fresh user 1% of market i's staked LT (as LT, or as gauge shares)"""
    lt, gauge = markets[i]
    user = boa.env.generate_address()
    amount = lt.balanceOf(gauge.address) // 100
    with boa.env.prank(gauge.address):
        lt.transfer(user, amount)
    if staked:
        with boa.env.prank(user):
            lt.approve(gauge.address, 2**256 - 1)
            amount = gauge.deposit(amount, user)
    return user, amount


def _migrate(migrator, markets, i, j, user, amount, staked):
    lt_from, gauge_from = markets[i]
    lt_to = markets[j][0]
    with boa.env.prank(user):
        if staked:
            gauge_from.approve(migrator.address, 2**256 - 1)
            migrator.migrate_staked(lt_from.address, lt_to.address, amount, 0)
        else:
            lt_from.approve(migrator.address, 2**256 - 1)
            migrator.migrate_plain(lt_from.address, lt_to.address, amount, 0)


@pytest.mark.parametrize("staked", [False, True])
@pytest.mark.parametrize("i,j", FORWARD)
def test_migrate_forward(migrator, owner, markets, i, j, staked):
    lt_from, gauge_from = markets[i]
    lt_to, gauge_to = markets[j]
    source, dest = (gauge_from, gauge_to) if staked else (lt_from, lt_to)
    user, amount = _fund(markets, i, staked)
    from_alloc, to_alloc = lt_from.stablecoin_allocation(), lt_to.stablecoin_allocation()
    disabled = owner.disabled_lts(lt_from.address)

    if staked:
        preview = migrator.preview_migrate_staked(lt_from.address, lt_to.address, amount)
    else:
        preview = migrator.preview_migrate_plain(lt_from.address, lt_to.address, amount)
    _migrate(migrator, markets, i, j, user, amount, staked)

    received = dest.balanceOf(user)
    assert source.balanceOf(user) == 0
    assert received > 0
    assert abs(received - preview) <= preview // 10**4
    assert lt_to.stablecoin_allocation() == to_alloc
    if disabled:
        assert lt_from.stablecoin_allocation() <= from_alloc
    else:
        assert lt_from.stablecoin_allocation() == from_alloc


@pytest.mark.parametrize("staked", [False, True])
@pytest.mark.parametrize("i,j,reason", REJECTED)
def test_migrate_rejected(migrator, markets, i, j, reason, staked):
    lt_from, lt_to = markets[i][0], markets[j][0]
    user, amount = _fund(markets, i, staked)
    from_alloc, to_alloc = lt_from.stablecoin_allocation(), lt_to.stablecoin_allocation()

    preview = migrator.preview_migrate_staked if staked else migrator.preview_migrate_plain
    with boa.reverts(reason):
        preview(lt_from.address, lt_to.address, amount)
    with boa.reverts(reason):
        _migrate(migrator, markets, i, j, user, amount, staked)
    assert lt_from.stablecoin_allocation() == from_alloc
    assert lt_to.stablecoin_allocation() == to_alloc


@pytest.mark.parametrize("staked", [False, True])
def test_migrate_out_of_disabled_v1(migrator, owner, markets, staked):
    """If the DAO ever manages to disable a v1 market, its LT still rejects the
    deallocation: the harvest is skipped and the migration goes through"""
    lt_from, lt_to = markets[0][0], markets[7][0]
    key = bytes.fromhex(lt_from.address[2:].rjust(64, "0"))
    slot = keccak(DISABLED_LTS_SLOT.to_bytes(32, "big") + key)
    boa.env.set_storage(owner.address, int.from_bytes(slot, "big"), 1)
    assert owner.disabled_lts(lt_from.address)

    user, amount = _fund(markets, 0, staked)
    from_alloc = lt_from.stablecoin_allocation()
    with boa.reverts("Not enough stables"):
        with boa.env.prank(migrator.address):
            owner.lt_allocate_stablecoins(lt_from.address, 0)

    _migrate(migrator, markets, 0, 7, user, amount, staked)
    dest = markets[7][1] if staked else lt_to
    assert dest.balanceOf(user) > 0
    assert lt_from.stablecoin_allocation() == from_alloc
