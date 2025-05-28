import boa
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule


WEEK = 7 * 86400
MAX_TIME = 86400 * 365 * 4
N_POOLS = 10
WEIGHT_VOTE_DELAY = 10 * 86400


# Fake gauge:
# get_adjustment()
# set_adjustment() (0..1)
# mint()

@pytest.fixture(scope="session")
def fake_gauges(mock_gov_token, gc, admin):
    gauge_deployer = boa.load_partial('contracts/testing/MockLiquidityGauge.vy')
    gauges = [gauge_deployer.deploy(mock_gov_token.address) for i in range(N_POOLS)]
    with boa.env.prank(admin):
        for gauge in gauges:
            gc.add_gauge(gauge.address)
    return gauges


class StatefulVE(RuleBasedStateMachine):
    USER_TOTAL = 10**40
    user_id = st.integers(min_value=0, max_value=9)
    amount = st.integers(min_value=1, max_value=10**40)
    lock_duration = st.integers(min_value=7 * 86400, max_value=MAX_TIME)
    dt = st.integers(min_value=0, max_value=30 * 86400)
    gauge_ids = st.lists(st.integers(min_value=0, max_value=N_POOLS - 1), min_size=0, max_size=N_POOLS)
    weight = st.integers(min_value=0, max_value=10001)

    def __init__(self):
        super().__init__()
        self.voting_balances = {}
        for user in self.accounts:
            with boa.env.prank(user):
                self.yb.approve(self.ve_yb.address, 2**256 - 1)
            with boa.env.prank(self.admin):
                self.yb.mint(user, self.USER_TOTAL)

    @rule(uid=user_id, amount=amount, lock_duration=lock_duration)
    def create_lock(self, uid, amount, lock_duration):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        unlock_time = t + lock_duration
        with boa.env.prank(user):
            if self.ve_yb.locked(user).amount > 0 or amount > self.yb.balanceOf(user):
                return
            else:
                self.ve_yb.create_lock(amount, unlock_time)

    @rule(uid=user_id, amount=amount)
    def increase_amount(self, uid, amount):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        with boa.env.prank(user):
            if self.ve_yb.locked(user).amount == 0 or self.ve_yb.locked(user).end <= t or amount > self.yb.balanceOf(user):
                return
            else:
                self.ve_yb.increase_amount(amount)

    @rule(uid=user_id, lock_duration=lock_duration)
    def increase_unlock_time(self, uid, lock_duration):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        unlock_time = t + lock_duration
        unlock_time_round = unlock_time // WEEK * WEEK
        prev_unlock_time = self.ve_yb.locked(user).end
        with boa.env.prank(user):
            if self.ve_yb.locked(user).amount == 0 or prev_unlock_time <= t or unlock_time_round <= prev_unlock_time or\
                    unlock_time_round > t + MAX_TIME:
                return
            else:
                self.ve_yb.increase_unlock_time(unlock_time)

    @rule(gauge_ids=gauge_ids, uid=user_id, weight=weight)
    def vote(self, gauge_ids, uid, weight):
        user = self.accounts[uid]
        gauges = [self.fake_gauges[gauge_id] for gauge_id in gauge_ids]
        weights = [weight] * len(gauges)
        t = boa.env.evm.patch.timestamp
        with boa.env.prank(user):
            if self.ve_yb.locked(user).end <= t:
                with boa.reverts("Expired"):
                    self.gc.vote_for_gauge_weights(gauges, weights)
            elif weight > 10000:
                with boa.reverts("Weight too large"):
                    self.gc.vote_for_gauge_weights(gauges, weights)
            elif len(set(gauge_ids)) < len(gauge_ids):
                try:
                    self.gc.vote_for_gauge_weights(gauges, weights)
                    raise Exception("Did not raise")
                except boa.BoaError as e:
                    err = str(e)
                    if "Cannot vote so often" in err or "Used too much power" in err:
                        return
                    else:
                        raise
            elif any(t < self.gc.last_user_vote(user, gauge) + WEIGHT_VOTE_DELAY for gauge in gauges):
                try:
                    self.gc.vote_for_gauge_weights(gauges, weights)
                    raise Exception("Did not raise")
                except boa.BoaError as e:
                    err = str(e)
                    if "Cannot vote so often" in err or "Used too much power" in err:
                        return
                    else:
                        raise
            elif len(gauge_ids) * weight + self.gc.vote_user_power(user) > 10000:
                with boa.reverts('Used too much power'):
                    self.gc.vote_for_gauge_weights(gauges, weights)
            else:
                self.gc.vote_for_gauge_weights(gauges, weights)

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)


def test_ve(ve_yb, yb, gc, fake_gauges, accounts, admin):
    StatefulVE.TestCase.settings = settings(max_examples=200, stateful_step_count=100)  # 2000, 100
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    run_state_machine_as_test(StatefulVE)
