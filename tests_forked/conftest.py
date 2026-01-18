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
