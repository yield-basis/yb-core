import boa
import pytest

from ..conftest import RESERVE, RATE  # noqa


N_POOLS = 5


@pytest.fixture(scope="session")
def mock_gov_token(token_mock):
    return token_mock.deploy('Valueless Governance Token', 'gov', 18)


@pytest.fixture(scope="session")
def mock_lp(mock_gov_token):
    return mock_gov_token


@pytest.fixture(scope="session")
def ve_mock(mock_gov_token, admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/VotingEscrow.vy', mock_gov_token.address, "veValueless", "veGov", "gov._yb.eth")


@pytest.fixture(scope="session")
def ve_yb(yb, admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/VotingEscrow.vy', yb.address, "veYB", "veYB", "gov._yb.eth")


@pytest.fixture(scope="session")
def gc(ve_yb, yb, admin):
    with boa.env.prank(admin):
        gc = boa.load('contracts/dao/GaugeController.vy', yb.address, ve_yb.address)
        yb.set_minter(gc.address, True)
        ve_yb.set_transfer_clearance_checker(gc.address)
        return gc


@pytest.fixture(scope="session")
def fake_gauges(mock_gov_token, gc, admin):
    gauge_deployer = boa.load_partial('contracts/testing/MockLiquidityGauge.vy')
    gauges = [gauge_deployer.deploy(mock_gov_token.address) for i in range(N_POOLS)]
    with boa.env.prank(admin):
        for gauge in gauges:
            gc.add_gauge(gauge.address)
    return gauges
