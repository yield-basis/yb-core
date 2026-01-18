import pytest
import boa
from tests_forked.networks import NETWORK


FACTORY_ADDRESS = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
# Aave V3 aToken contracts (hold underlying tokens)
WETH_WHALE = "0x4d5F47FA6A74757f35C14fD3a6Ef8E3C9BC514E8"  # aWETH
WBTC_WHALE = "0x5Ee5bf7ae06D1Be5997A1A72006FE6C607eC6DE8"  # aWBTC


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
    """
    Generate a random account and fund it with WETH and WBTC
    by impersonating Aave aToken contracts.
    """
    account = boa.env.generate_address()
    erc20 = boa.load_partial("contracts/testing/ERC20Mock.vy")

    weth = erc20.at(WETH)
    wbtc = erc20.at(WBTC)

    # Steal 100 WETH from aWETH contract
    with boa.env.prank(WETH_WHALE):
        weth.transfer(account, 100 * 10**18)

    # Steal 10 WBTC from aWBTC contract
    with boa.env.prank(WBTC_WHALE):
        wbtc.transfer(account, 10 * 10**8)

    return account
