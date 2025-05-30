import boa
import pytest
from math import log
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, invariant


WEEK = 7 * 86400
MAX_TIME = 86400 * 365 * 4
N_POOLS = 5
WEIGHT_VOTE_DELAY = 10 * 86400


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
    gauge_id = st.integers(min_value=0, max_value=N_POOLS - 1)
    adjustment = st.integers(min_value=0, max_value=10**18)

    def __init__(self):
        super().__init__()
        self.voting_balances = {}
        for user in self.accounts:
            with boa.env.prank(user):
                self.yb.approve(self.ve_yb.address, 2**256 - 1)
            with boa.env.prank(self.admin):
                self.yb.mint(user, self.USER_TOTAL)

    @rule(gauge_id=gauge_id, adj=adjustment)
    def set_adjustment(self, gauge_id, adj):
        self.fake_gauges[gauge_id].set_adjustment(adj)

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
            elif any(self.gc.is_killed(g) for g in gauges) and weight > 0 and len(gauge_ids) > 0:
                with boa.reverts():
                    self.gc.vote_for_gauge_weights(gauges, weights)
            elif len(gauge_ids) > 0 and weight > 10000:
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
            elif (len(gauge_ids) * weight
                  - sum(self.gc.vote_user_slopes(user, self.fake_gauges[g].address).power for g in gauge_ids)
                  + self.gc.vote_user_power(user)) > 10000:
                with boa.reverts('Used too much power'):
                    self.gc.vote_for_gauge_weights(gauges, weights)
            else:
                self.gc.vote_for_gauge_weights(gauges, weights)

    @rule(gauge_id=gauge_id)
    def checkpoint(self, gauge_id):
        self.gc.checkpoint(self.fake_gauges[gauge_id])

    @rule(gauge_id=gauge_id)
    def emit(self, gauge_id):
        t = boa.env.evm.patch.timestamp
        gauge = self.fake_gauges[gauge_id]
        expected_emissions = self.gc.preview_emissions(gauge, t)
        with boa.env.prank(gauge.address):
            before = self.yb.balanceOf(gauge.address)
            self.gc.emit()
            after = self.yb.balanceOf(gauge.address)
        assert after - before == expected_emissions

    @rule(gauge_id=gauge_id)
    def kill_toggle(self, gauge_id):
        gauge = self.fake_gauges[gauge_id]
        with boa.env.prank(self.admin):
            self.gc.set_killed(gauge.address, not self.gc.is_killed(gauge.address))

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)

    @invariant()
    def check_sum_votes(self):
        sum_adj_weight = self.gc.adjusted_gauge_weight_sum()
        min_sum_adj_weight = min(sum_adj_weight - self.gc.adjusted_gauge_weight(g.address) for g in self.fake_gauges)
        uncertainty = 100 * N_POOLS / max(min_sum_adj_weight, 1e-10)
        if sum_adj_weight > 0:
            sum_votes = sum(self.gc.gauge_relative_weight(g.address) for g in self.fake_gauges)
            adj = [g.get_adjustment() for g in self.fake_gauges]
            aw = [self.gc.adjusted_gauge_weight(g.address) for g in self.fake_gauges]
            gw = [self.gc.get_gauge_weight(g.address) for g in self.fake_gauges]
            agws = self.gc.adjusted_gauge_weight_sum()
            if sum_votes == 0:
                assert uncertainty > 0.5 or sum(g * a // 10**18 for (g, a) in zip(gw, adj)) == 0
            else:
                assert min(abs(log(sum_votes / 1e18)), 1) <= uncertainty

    def teardown(self):
        # Check that all votes go to zero after long enough time
        boa.env.time_travel(MAX_TIME)
        for g in self.fake_gauges:
            self.gc.checkpoint(g.address)
        for g in self.fake_gauges:
            assert self.gc.get_gauge_weight(g.address) == 0
        super().teardown()


def test_ve(ve_yb, yb, gc, fake_gauges, accounts, admin):
    StatefulVE.TestCase.settings = settings(max_examples=2000, stateful_step_count=100)  # 2000, 100
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    run_state_machine_as_test(StatefulVE)


def test_ve_one(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.set_adjustment(adj=21944, gauge_id=0)
    state.create_lock(amount=12_171_973_003_973_568_000, lock_duration=70681194, uid=8)
    state.vote(gauge_ids=[0, 1], uid=8, weight=644)
    state.set_adjustment(adj=1, gauge_id=0)
    state.check_sum_votes()
    state.teardown()


def test_ve_two(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.check_sum_votes()
    state.create_lock(amount=1, lock_duration=604951, uid=9)
    state.check_sum_votes()
    state.create_lock(amount=6_331_097_503_943_142_998_954_948_577_033_393_413_455, lock_duration=604800, uid=0)
    state.check_sum_votes()
    state.checkpoint(gauge_id=0)
    state.check_sum_votes()
    state.checkpoint(gauge_id=1)
    state.check_sum_votes()
    state.checkpoint(gauge_id=0)
    state.check_sum_votes()
    state.vote(gauge_ids=[0, 1], uid=0, weight=1196)
    state.check_sum_votes()
    state.set_adjustment(adj=56, gauge_id=0)
    state.check_sum_votes()
    state.set_adjustment(adj=73, gauge_id=1)
    state.check_sum_votes()
    state.checkpoint(gauge_id=0)
    state.check_sum_votes()
    state.checkpoint(gauge_id=1)
    state.check_sum_votes()
    state.time_travel(dt=466211)
    state.check_sum_votes()
    state.teardown()


def test_ve_three(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.check_sum_votes()
    state.increase_amount(amount=1, uid=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=1)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=3)
    state.check_sum_votes()
    state.increase_amount(amount=1, uid=0)
    state.check_sum_votes()
    state.increase_amount(amount=1, uid=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=3)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=3)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=1)
    state.check_sum_votes()
    state.vote(gauge_ids=[], uid=0, weight=0)
    state.check_sum_votes()
    state.increase_amount(amount=1, uid=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=1)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=1)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=3)
    state.check_sum_votes()
    state.increase_amount(amount=1, uid=0)
    state.check_sum_votes()
    state.increase_unlock_time(lock_duration=604800, uid=0)
    state.check_sum_votes()
    state.increase_unlock_time(lock_duration=604800, uid=0)
    state.check_sum_votes()
    state.increase_unlock_time(lock_duration=604800, uid=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=0)
    state.check_sum_votes()
    state.create_lock(amount=1, lock_duration=604800, uid=0)
    state.check_sum_votes()
    state.set_adjustment(adj=121_629_090_225_432_490, gauge_id=0)
    state.check_sum_votes()
    state.increase_amount(amount=1, uid=0)
    state.check_sum_votes()
    state.increase_amount(amount=1_261_439_999_998, uid=0)
    state.check_sum_votes()
    state.kill_toggle(gauge_id=2)
    state.check_sum_votes()
    state.set_adjustment(adj=493_499_187_768_888_746, gauge_id=3)
    state.check_sum_votes()
    state.vote(gauge_ids=[0, 0, 0, 0, 0], uid=0, weight=5458)
    state.check_sum_votes()
    state.increase_amount(amount=604800, uid=0)
    state.check_sum_votes()
    state.vote(gauge_ids=[0, 3], uid=0, weight=1)
    state.check_sum_votes()
    state.set_adjustment(adj=0, gauge_id=0)
    state.check_sum_votes()
    state.teardown()
