import pytest
import boa
from .conftest import N_POOLS


VOTES = [v * 10000 // sum(range(N_POOLS)) for v in range(N_POOLS)]


@pytest.fixture(scope="session")
def dummy_factory(gc, admin):
    return boa.load('contracts/testing/DummyFactoryForGauge.vy', admin, gc.address)


@pytest.fixture(scope="session")
def gauges(mock_lp, gc, dummy_factory, admin):
    gauge_deployer = boa.load_partial('contracts/dao/LiquidityGauge.vy')
    with boa.env.prank(dummy_factory.address):
        gauges = [gauge_deployer.deploy(mock_lp.address) for i in range(N_POOLS)]
    with boa.env.prank(admin):
        for gauge in gauges:
            gc.add_gauge(gauge.address)
    return gauges


@pytest.fixture(scope="session")
def vote_for_gauges(gauges, yb, ve_yb, gc, accounts, admin):
    user = accounts[0]
    t = boa.env.evm.patch.timestamp
    with boa.env.prank(admin):
        yb.mint(user, 10**18)
    with boa.env.prank(user):
        yb.approve(ve_yb.address, 2**256 - 1)
        ve_yb.create_lock(10**18, t + 4 * 365 * 86400)
        gc.vote_for_gauge_weights(gauges, VOTES)


def test_test(gauges, gc, accounts, vote_for_gauges):
    pass
