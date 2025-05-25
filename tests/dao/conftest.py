import boa
import pytest

from ..conftest import RESERVE, RATE  # noqa


@pytest.fixture(scope="session")
def mock_gov_token(token_mock):
    return token_mock.deploy('Valueless Governance Token', 'gov', 18)


@pytest.fixture(scope="session")
def ve_mock(mock_gov_token, admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/VotingEscrow.vy', mock_gov_token.address, "veValueless", "veGov", "gov._yb.eth")
