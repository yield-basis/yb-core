import boa
import pytest


RESERVE = 10**9
RATE = 10**9 // (365 * 86400)


@pytest.fixture(scope="session")
def yb(admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/YB.vy', RESERVE, RATE)


@pytest.fixture(scope="session")
def mock_gov_token(token_mock):
    return token_mock.deploy('Valueless Governance Token', 'gov', 18)


@pytest.fixture(scope="session")
def ve_mock(mock_gov_token, admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/VotingEscrow.vy', mock_gov_token.address, "veValueless", "veGov", "gov._yb.eth")
