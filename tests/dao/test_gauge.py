import pytest
import boa
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, invariant
from .conftest import N_POOLS


VOTES = [v * 10000 // sum(range(N_POOLS)) for v in range(N_POOLS)]


@pytest.fixture(scope="session")
def dummy_factory(gc, admin):
    return boa.load('contracts/testing/DummyFactoryForGauge.vy', admin, gc.address)


@pytest.fixture(scope="session")
def gauges(mock_lp, gc, dummy_factory, admin, accounts):
    gauge_deployer = boa.load_partial('contracts/dao/LiquidityGauge.vy')
    with boa.env.prank(dummy_factory.address):
        gauges = [gauge_deployer.deploy(mock_lp.address) for i in range(N_POOLS)]
    with boa.env.prank(admin):
        for gauge in gauges:
            gc.add_gauge(gauge.address)
    for user in accounts:
        with boa.env.prank(user):
            for g in gauges:
                mock_lp.approve(g.address, 2**256 - 1)
                mock_lp._mint_for_testing(user, 10**40)
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


class StatefulG(RuleBasedStateMachine):
    user_id = st.integers(min_value=0, max_value=9)
    gauge_id = st.integers(min_value=0, max_value=N_POOLS - 1)
    token_amount = st.integers(min_value=0, max_value=10**25)
    dt = st.integers(min_value=0, max_value=30 * 86400)

    @rule(uid=user_id, assets=token_amount, gid=gauge_id)
    def deposit(self, uid, assets, gid):
        user = self.accounts[uid]
        with boa.env.prank(user):
            self.gauges[gid].deposit(assets, user)

    @rule(uid=user_id, shares=token_amount, gid=gauge_id)
    def withdraw(self, uid, shares, gid):
        user = self.accounts[uid]
        with boa.env.prank(user):
            if shares <= self.gauges[gid].balanceOf(user):
                self.gauges[gid].redeem(shares, user, user)

    def transfer(self):
        pass

    def claim(self):
        pass

    @invariant()
    def check_adjustment(self):
        supply = self.mock_lp.totalSupply()
        for g in self.gauges:
            measured_adjustment = g.get_adjustment()
            assert measured_adjustment <= 10**18
            bal = self.mock_lp.balanceOf(g.address)
            if supply == 0:
                assert measured_adjustment == 0
            else:
                assert abs((bal / supply)**0.5 - measured_adjustment / 1e18) < 1e-9

    def check_mint_sum(self):
        pass

    def check_mint_split(self):
        pass

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)


def test_gauges(mock_lp, gauges, gc, accounts, vote_for_gauges):
    StatefulG.TestCase.settings = settings(max_examples=100, stateful_step_count=100)
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    run_state_machine_as_test(StatefulG)
