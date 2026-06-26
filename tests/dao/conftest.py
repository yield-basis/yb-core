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
def voting_escrow_deployer():
    # Compile VotingEscrow once; .deploy() per instance instead of re-loading.
    return boa.load_partial('contracts/dao/VotingEscrow.vy')


@pytest.fixture(scope="session")
def ve_mock(voting_escrow_deployer, mock_gov_token, admin):
    with boa.env.prank(admin):
        return voting_escrow_deployer.deploy(mock_gov_token.address, "veValueless", "veGov", "gov._yb.eth")


@pytest.fixture(scope="session")
def ve_yb(voting_escrow_deployer, yb, admin):
    with boa.env.prank(admin):
        return voting_escrow_deployer.deploy(yb.address, "veYB", "veYB", "gov._yb.eth")


@pytest.fixture(scope="session")
def liquidity_gauge_deployer():
    # Compile LiquidityGauge once; .deploy() per instance instead of re-loading.
    return boa.load_partial('contracts/dao/LiquidityGauge.vy')


@pytest.fixture(scope="session")
def dummy_factory_deployer():
    # Compile DummyFactoryForGauge once; .deploy() per instance.
    return boa.load_partial('contracts/testing/DummyFactoryForGauge.vy')


@pytest.fixture(scope="session")
def gc(ve_yb, yb, admin):
    with boa.env.prank(admin):
        gc = boa.load('contracts/dao/GaugeController.vy', yb.address, ve_yb.address)
        yb.set_minter(gc.address, True)
        ve_yb.set_transfer_clearance_checker(gc.address)
        return gc
