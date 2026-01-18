import pytest
import boa
from tests_forked.networks import NETWORK


FACTORY_ADDRESS = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
SCRVUSD = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    """Fork the network defined in networks.py for all tests in this module."""
    with boa.fork(NETWORK):
        yield


@pytest.fixture(scope="module")
def factory(forked_env):
    """Load the Factory contract at its deployed address."""
    return boa.load_partial("contracts/Factory.vy").at(FACTORY_ADDRESS)


@pytest.fixture(scope="module")
def hybrid_factory_owner(factory):
    """
    Transfer Factory ownership from MigrationFactoryOwner -> DAO -> HybridFactoryOwner.
    Returns the newly deployed HybridFactoryOwner with DAO as admin.
    """
    migration_owner = boa.load_partial("contracts/MigrationFactoryOwner.vy").at(factory.admin())
    dao = migration_owner.ADMIN()
    emergency_admin = factory.emergency_admin()

    # Transfer Factory back to DAO
    with boa.env.prank(dao):
        migration_owner.transfer_ownership_back()

    # Deploy HybridFactoryOwner with DAO as admin
    hybrid_owner = boa.load("contracts/HybridFactoryOwner.vy", dao, factory.address)

    # Transfer Factory to HybridFactoryOwner
    with boa.env.prank(dao):
        factory.set_admin(hybrid_owner.address, emergency_admin)

    return hybrid_owner


@pytest.fixture(scope="module")
def dao(hybrid_factory_owner):
    """Extract DAO address from HybridFactoryOwner."""
    return hybrid_factory_owner.ADMIN()


@pytest.fixture(scope="module")
def funded_account(forked_env):
    """Generate a random account and fund it with WETH, WBTC, and crvUSD."""
    account = boa.env.generate_address()
    boa.deal(WETH, account, 100 * 10**18)
    boa.deal(WBTC, account, 10 * 10**8)
    boa.deal(CRVUSD, account, 1_000_000 * 10**18)
    return account


@pytest.fixture(scope="module")
def hybrid_vault_factory(factory, hybrid_factory_owner):
    """Deploy HybridVaultFactory with pools 3 and 6, each with 50M limit."""
    vault_impl = boa.load(
        "contracts/HybridVault.vy",
        factory.address,
        CRVUSD,
        SCRVUSD
    )
    return boa.load(
        "contracts/HybridVaultFactory.vy",
        factory.address,
        vault_impl.address,
        [3, 6],
        [50_000_000 * 10**18, 50_000_000 * 10**18]
    )
