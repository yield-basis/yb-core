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
    erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")

    # WETH: give ETH and wrap it
    boa.env.set_balance(account, 100 * 10**18)
    weth = boa.load_partial("contracts/testing/WETH.vy").at(WETH)
    with boa.env.prank(account):
        weth.deposit(value=100 * 10**18)

    # WBTC and crvUSD: use boa.deal
    boa.deal(erc20.at(WBTC), account, 10 * 10**8)
    boa.deal(erc20.at(CRVUSD), account, 1_000_000 * 10**18)
    return account


@pytest.fixture(scope="module")
def hybrid_vault_factory(factory, hybrid_factory_owner, dao):
    """Deploy HybridVaultFactory with pools 3 and 6, each with 300M limit."""
    # Deploy factory first (without impl)
    vault_factory = boa.load(
        "contracts/HybridVaultFactory.vy",
        factory.address,
        [3, 6],
        [300_000_000 * 10**18, 300_000_000 * 10**18]
    )

    # Deploy vault impl with vault_factory address
    vault_impl = boa.load(
        "contracts/HybridVault.vy",
        factory.address,
        CRVUSD,
        vault_factory.address
    )

    # Set vault impl on factory
    with boa.env.prank(dao):
        vault_factory.set_vault_impl(vault_impl.address)
        hybrid_factory_owner.set_limit_setter(vault_factory.address, True)
        vault_factory.set_allowed_crvusd_vault(SCRVUSD, True)

    return vault_factory


@pytest.fixture(scope="module")
def twocrypto(forked_env):
    """Twocrypto interface for cryptopool interactions."""
    return boa.load_partial("contracts/testing/twocrypto/Twocrypto.vy")


@pytest.fixture(scope="module")
def hybrid_vault_deployer(forked_env):
    """HybridVault contract interface for deploying/loading vaults."""
    return boa.load_partial("contracts/HybridVault.vy")


@pytest.fixture(scope="module")
def vault(hybrid_vault_factory, hybrid_vault_deployer, funded_account):
    """Create a HybridVault for the funded_account."""
    with boa.env.prank(funded_account):
        vault_addr = hybrid_vault_factory.create_vault(SCRVUSD)
    return hybrid_vault_deployer.at(vault_addr)


@pytest.fixture(scope="module")
def erc20(forked_env):
    """ERC20 interface for token interactions."""
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def wbtc(erc20):
    return erc20.at(WBTC)


@pytest.fixture(scope="module")
def weth(erc20):
    return erc20.at(WETH)


@pytest.fixture(scope="module")
def crvusd(erc20):
    return erc20.at(CRVUSD)


@pytest.fixture(scope="module")
def setup_approvals(vault, funded_account, wbtc, weth, crvusd):
    """Approve vault to spend user's tokens."""
    with boa.env.prank(funded_account):
        wbtc.approve(vault.address, 2**256 - 1)
        weth.approve(vault.address, 2**256 - 1)
        crvusd.approve(vault.address, 2**256 - 1)
