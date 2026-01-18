import pytest
import boa
from tests_forked.networks import NETWORK


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    """Fork the network defined in networks.py for all tests in this module."""
    with boa.fork(NETWORK):
        yield
