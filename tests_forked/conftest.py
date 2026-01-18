import pytest
import boa
from tests_forked.networks import NETWORK


FACTORY_ADDRESS = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"


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
