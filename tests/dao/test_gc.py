import boa
import pytest
import os
from math import log
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis import given
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, invariant
from .conftest import N_POOLS


WEEK = 7 * 86400
MAX_TIME = 86400 * 365 * 4
WEIGHT_VOTE_DELAY = 10 * 86400


@pytest.fixture(scope="session")
def fake_gauges(mock_gov_token, gc, admin):
    gauge_deployer = boa.load_partial('contracts/testing/MockLiquidityGauge.vy')
    gauges = [gauge_deployer.deploy(mock_gov_token.address) for i in range(N_POOLS)]
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
        for user in self.accounts:
            with boa.env.prank(user):
                self.yb.approve(self.ve_yb.address, 2**256 - 1)
            with boa.env.prank(self.admin):
                self.yb.mint(user, self.USER_TOTAL)
        self.added_gauges = []
        self.addition_times = {}

    @rule(gauge_id=gauge_id)
    def add_gauge(self, gauge_id):
        gauge = self.fake_gauges[gauge_id]

        with boa.env.prank(self.admin):
            if gauge in self.added_gauges:
                with boa.reverts():
                    self.gc.add_gauge(gauge.address)
            else:
                self.gc.add_gauge(gauge.address)
                self.added_gauges.append(gauge)
                self.addition_times[gauge] = boa.env.evm.patch.timestamp

    @rule(gauge_id=gauge_id, adj=adjustment)
    def set_adjustment(self, gauge_id, adj):
        gauge = self.fake_gauges[gauge_id]
        gauge.set_adjustment(adj)
        if gauge in self.added_gauges:
            self.gc.checkpoint(gauge)

    @rule(uid=user_id, amount=amount, lock_duration=lock_duration)
    def create_lock(self, uid, amount, lock_duration):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        unlock_time = t + lock_duration
        with boa.env.prank(user):
            if self.ve_yb.locked(user).amount > 0 or amount > self.yb.balanceOf(user) or amount < MAX_TIME:
                return
            else:
                self.ve_yb.create_lock(amount, unlock_time)

    @rule(uid=user_id, amount=amount)
    def increase_amount(self, uid, amount):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        with boa.env.prank(user):
            if self.ve_yb.locked(user).amount == 0 or self.ve_yb.locked(user).end <= t or\
                    amount > self.yb.balanceOf(user) or amount < MAX_TIME:
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

    @rule(uid=user_id)
    def infinite_lock_toggle(self, uid):
        user = self.accounts[uid]
        with boa.env.prank(user):
            try:
                self.ve_mock.infinite_lock_toggle()
            except Exception:
                # it's ok if lock does not exist - we are not testing the infilock itself
                pass

    def _check_weight_too_much(self, gauge_ids, weight, user):
        gauge_ids = [g for g in gauge_ids if g < len(self.added_gauges)]
        user_power = self.gc.vote_user_power(user)
        old_powers = [self.gc.vote_user_slopes(user, self.added_gauges[g].address).power for g in gauge_ids]
        for old_weight in old_powers:
            user_power += weight - old_weight
            if user_power > 10000:
                return True
        return False

    @rule(gauge_ids=gauge_ids, uid=user_id, weight=weight)
    def vote(self, gauge_ids, uid, weight):
        gauge_ids = [g for g in gauge_ids if g < len(self.added_gauges)]
        user = self.accounts[uid]
        gauges = [self.added_gauges[gauge_id] for gauge_id in gauge_ids]
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
            elif self._check_weight_too_much(gauge_ids, weight, user):
                with boa.reverts('Used too much power'):
                    self.gc.vote_for_gauge_weights(gauges, weights)
            else:
                self.gc.vote_for_gauge_weights(gauges, weights)

    @rule(uid1=user_id, uid2=user_id)
    def merge(self, uid1, uid2):
        user1 = self.accounts[uid1]
        user2 = self.accounts[uid2]
        max_time = (boa.env.evm.patch.timestamp + MAX_TIME) // WEEK * WEEK
        amount1, t1 = self.ve_yb.locked(user1)
        amount2, t2 = self.ve_yb.locked(user2)
        if amount1 == 0:
            with boa.reverts():
                self.ve_yb.tokenOfOwnerByIndex(user1, 0)
            return
        id1 = self.ve_yb.tokenOfOwnerByIndex(user1, 0)
        with boa.env.prank(user1):
            if user1 == user2:
                with boa.reverts():
                    self.ve_yb.transferFrom(user1, user2, id1)
            elif amount2 == 0:
                with boa.reverts():
                    self.ve_yb.transferFrom(user1, user2, id1)
            elif self.gc.vote_user_power(user1) != 0:
                with boa.reverts("Not allowed"):
                    self.ve_yb.transferFrom(user1, user2, id1)
            elif t1 != max_time or t2 != max_time:
                with boa.reverts("Need max veLock"):
                    self.ve_yb.transferFrom(user1, user2, id1)
            else:
                self.ve_yb.transferFrom(user1, user2, id1)

    @rule(gauge_id=gauge_id)
    def checkpoint(self, gauge_id):
        if gauge_id < len(self.added_gauges):
            self.gc.checkpoint(self.added_gauges[gauge_id])

    @rule(gauge_id=gauge_id)
    def emit(self, gauge_id):
        t = boa.env.evm.patch.timestamp
        gauge = self.fake_gauges[gauge_id]
        expected_emissions = self.gc.preview_emissions(gauge, t)
        with boa.env.prank(gauge.address):
            before = self.yb.balanceOf(gauge.address)
            if gauge in self.added_gauges:
                self.gc.emit()
                # If emissions were live before the very first vote -
                # accumulated emissions are split between all gauges voted,
                # otherwise new gauge doesn't get anything before the time passes
                if t == self.addition_times[gauge] and self.gc.specific_emissions() > 0:
                    assert expected_emissions == 0
            else:
                with boa.reverts():
                    self.gc.emit()
                    return
            after = self.yb.balanceOf(gauge.address)
        assert after - before == expected_emissions

    @rule(gauge_id=gauge_id)
    def kill_toggle(self, gauge_id):
        gauge = self.fake_gauges[gauge_id]
        with boa.env.prank(self.admin):
            if gauge in self.added_gauges:
                self.gc.set_killed(gauge.address, not self.gc.is_killed(gauge.address))
            else:
                with boa.reverts("Gauge not added"):
                    self.gc.set_killed(gauge.address, not self.gc.is_killed(gauge.address))

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)

    @invariant()
    def check_sum_votes(self):
        sum_adj_weight = self.gc.adjusted_gauge_weight_sum()
        uncertainty = 100 * N_POOLS / max(sum_adj_weight, 1e-10)
        if sum_adj_weight > 0:
            sum_votes = sum(self.gc.gauge_relative_weight(g.address) for g in self.added_gauges)
            assert sum_votes <= 10**18
            if sum_votes == 0:
                assert uncertainty > 0.5 or sum(self.gc.adjusted_gauge_weight(g.address) for g in self.added_gauges) == 0
            else:
                assert min(abs(log(sum_votes / 1e18)), 1) <= uncertainty

    def teardown(self):
        # Check that all votes go to zero after long enough time
        boa.env.time_travel(MAX_TIME)
        for g in self.added_gauges:
            self.gc.checkpoint(g.address)
        for g in self.added_gauges:
            assert self.gc.get_gauge_weight(g.address) == 0
        super().teardown()


@pytest.mark.parametrize("_tmp", range(int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", 1))))  # This splits the test into small chunks which are easier to parallelize
def test_gc(ve_yb, yb, gc, fake_gauges, accounts, admin, _tmp):
    StatefulVE.TestCase.settings = settings(max_examples=1000, stateful_step_count=100)  # 2000, 100
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    run_state_machine_as_test(StatefulVE)


def test_gc_one(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.set_adjustment(adj=21944, gauge_id=0)
    state.create_lock(amount=12_171_973_003_973_568_000, lock_duration=70681194, uid=8)
    state.vote(gauge_ids=[0, 1], uid=8, weight=644)
    state.set_adjustment(adj=1, gauge_id=0)
    state.check_sum_votes()
    state.teardown()


def test_gc_two(ve_yb, yb, gc, fake_gauges, accounts, admin):
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


def test_gc_three(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.create_lock(amount=1_261_440_000_000, lock_duration=604800, uid=0)
    state.set_adjustment(adj=121_629_090_225_432_490, gauge_id=0)
    state.set_adjustment(adj=493_499_187_768_888_746, gauge_id=3)
    state.vote(gauge_ids=[0, 3], uid=0, weight=1)
    state.set_adjustment(adj=0, gauge_id=0)
    state.check_sum_votes()
    state.teardown()


def test_gc_four(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.vote(gauge_ids=[0, 0], uid=0, weight=0)
    state.increase_unlock_time(lock_duration=604800, uid=0)
    state.time_travel(dt=3)
    state.create_lock(amount=1, lock_duration=604800, uid=0)
    state.increase_unlock_time(lock_duration=604800, uid=0)
    state.increase_unlock_time(lock_duration=104379306, uid=0)
    state.create_lock(amount=1, lock_duration=604800, uid=0)
    state.increase_unlock_time(lock_duration=604800, uid=0)
    state.create_lock(amount=1, lock_duration=604800, uid=0)
    state.vote(gauge_ids=[2], uid=0, weight=10000)
    state.time_travel(dt=2341952)
    state.vote(gauge_ids=[0, 4, 2], uid=0, weight=444)
    state.teardown()


def test_gc_merge(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.check_sum_votes()
    state.create_lock(amount=1, lock_duration=604800, uid=1)
    state.check_sum_votes()
    state.create_lock(amount=1, lock_duration=604800, uid=0)
    state.check_sum_votes()
    state.vote(gauge_ids=[0], uid=0, weight=1)
    state.check_sum_votes()
    state.merge(uid1=0, uid2=1)
    state.teardown()


def test_gc_nonzero_emissions(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.add_gauge(gauge_id=0)
    state.set_adjustment(adj=144, gauge_id=1)
    state.time_travel(dt=998784)
    state.add_gauge(gauge_id=3)
    state.emit(gauge_id=0)
    state.add_gauge(gauge_id=2)
    state.time_travel(dt=1716320)
    state.create_lock(amount=4_354_078_040_879_253_557_973_226_741_716_485_915_238, lock_duration=640600, uid=9)
    state.add_gauge(gauge_id=1)
    state.vote(gauge_ids=[3], uid=9, weight=2284)
    state.emit(gauge_id=1)
    state.teardown()


def test_emit_expected_emissions(ve_yb, yb, gc, fake_gauges, accounts, admin):
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.add_gauge(gauge_id=4)  # 0
    state.add_gauge(gauge_id=1)
    state.time_travel(dt=991258)
    fake_gauges[0].set_adjustment(10**18)  # Can also be after add_gauge
    state.add_gauge(gauge_id=3)
    state.add_gauge(gauge_id=0)  # 3
    state.create_lock(amount=2_425_115_004_361_743_762, lock_duration=112685815, uid=0)
    state.vote(gauge_ids=[3, 0], uid=0, weight=5000)
    state.emit(gauge_id=0)
    state.teardown()


@pytest.fixture(scope="session")
def lock_for_accounts(yb, ve_yb, accounts, admin):
    with boa.env.prank(admin):
        for user in accounts:
            yb.mint(user, 10**18)
    end_lock = boa.env.evm.patch.timestamp + MAX_TIME
    for user in accounts:
        with boa.env.prank(user):
            yb.approve(ve_yb.address, 2**256-1)
            ve_yb.create_lock(10**18, end_lock)


@pytest.fixture(scope="session")
def prepare_gauges(fake_gauges):
    for gauge in fake_gauges:
        gauge.set_adjustment(10**18)


@given(
        vote_split=st.lists(
            st.lists(
                st.integers(min_value=0, max_value=10000),
                min_size=N_POOLS, max_size=N_POOLS),
            min_size=10, max_size=10)
)
def test_vote_split(fake_gauges, gc, accounts, lock_for_accounts, prepare_gauges, vote_split, admin):
    with boa.env.prank(admin):
        for gauge in fake_gauges:
            gc.add_gauge(gauge.address)
    if sum(sum(v) for v in vote_split) == 0:
        return
    vote_tracker = {g: 0 for g in fake_gauges}
    for votes, user in zip(vote_split, accounts):
        with boa.env.prank(user):
            if sum(votes) > 10000:
                with boa.reverts('Used too much power'):
                    gc.vote_for_gauge_weights(fake_gauges, votes)
                return
            else:
                gc.vote_for_gauge_weights(fake_gauges, votes)
                for gauge, vote in zip(fake_gauges, votes):
                    vote_tracker[gauge] += vote

    sum_votes = sum(vote_tracker.values())
    vote_tracker = {g: v / sum_votes for g, v in vote_tracker.items()}
    for g, v in vote_tracker.items():
        rw = gc.gauge_relative_weight(g) / 1e18
        assert abs(rw - v) < 1e-12

    dt = MAX_TIME // 40

    t_passed = 0
    initial_aw = {g: gc.adjusted_gauge_weight(g.address) for g in fake_gauges}
    initial_aws = gc.adjusted_gauge_weight_sum()
    for i in range(50):
        boa.env.time_travel(dt)
        t_passed += dt
        for g in fake_gauges:
            gc.checkpoint(g.address)
        aws = gc.adjusted_gauge_weight_sum()
        assert abs(aws - initial_aws * max(1 - t_passed / MAX_TIME, 0)) < initial_aws * (7 * 86400) / MAX_TIME
        for g in fake_gauges:
            rw = gc.gauge_relative_weight(g.address) / 1e18
            aw = gc.adjusted_gauge_weight(g.address)
            expected_weight = initial_aw[g] * max(1 - t_passed / MAX_TIME, 0)
            if rw != 0:
                assert abs(rw - vote_tracker[g]) < 1e-12
                assert abs(aw - expected_weight) < initial_aw[g] * (7 * 86400) / MAX_TIME


@pytest.mark.skip(reason="Only to be used as a script for manual checking")
@pytest.mark.parametrize("use_flashloan", [False, True], ids=["no_flashloan", "with_flashloan"])
def test_gc_total_supply_manip(ve_yb, yb, gc, accounts, admin, token_mock, use_flashloan):
    """
    Test gauge controller behavior with total supply manipulation via flashloan-like operations.

    Args:
        use_flashloan: Whether to simulate flashloan operations (deposit/withdraw large amounts)
    """
    user = accounts[0]
    user2 = accounts[1]

    lp_token1 = token_mock.deploy('LP Token 1', 'LP1', 18)
    lp_token2 = token_mock.deploy('LP Token 2', 'LP2', 18)

    # Create DummyFactory for deploying real LiquidityGauge contracts
    dummy_factory = boa.load('contracts/testing/DummyFactoryForGauge.vy', admin, gc.address)
    gauge_factory = boa.load_partial('contracts/dao/LiquidityGauge.vy')

    # Create two real LiquidityGauge contracts through Factory
    with boa.env.prank(dummy_factory.address):
        gauge1 = gauge_factory.deploy(lp_token1.address)
        gauge2 = gauge_factory.deploy(lp_token2.address)

    # Add gauges to GaugeController and mint tokens (from admin)
    with boa.env.prank(admin):
        gc.add_gauge(gauge1.address)
        gc.add_gauge(gauge2.address)
        yb.mint(user, 10**18)

        # Initialize YB token emission
        # Mint LP tokens to users for staking
        lp_token1._mint_for_testing(user, 100 * 10**18)
        lp_token1._mint_for_testing(user2, 100 * 10**18)
        lp_token2._mint_for_testing(user, 100 * 10**18)
        lp_token2._mint_for_testing(user2, 100 * 10**18)

    # Create lock with MAX_TIME for user
    end_lock = boa.env.evm.patch.timestamp + MAX_TIME
    with boa.env.prank(user):
        yb.approve(ve_yb.address, 2**256-1)
        ve_yb.create_lock(10**18, end_lock)

        # Stake LP tokens in both gauges to receive emissions
        lp_token1.approve(gauge1.address, 2**256-1)
        lp_token2.approve(gauge2.address, 2**256-1)
        gauge1.deposit(90 * 10**18, user)  # Stake 90 LP1 tokens
        gauge2.deposit(90 * 10**18, user)  # Stake 90 LP2 tokens

    # Skip time 30 days
    boa.env.time_travel(30 * 86400)

    # Vote equally for both gauges (5000 each)
    gauges = [gauge1.address, gauge2.address]
    weights = [5000, 5000]

    with boa.env.prank(user):
        gc.vote_for_gauge_weights(gauges, weights)

    # Check initial weights
    initial_weight1 = gc.get_gauge_weight(gauge1.address)
    initial_weight2 = gc.get_gauge_weight(gauge2.address)
    print(f"Initial weights: gauge1={initial_weight1}, gauge2={initial_weight2}")
    print(f"Initial adjusted weights: gauge1={gc.adjusted_gauge_weight(gauge1.address)}, gauge2={gc.adjusted_gauge_weight(gauge2.address)}")
    print(f"Initial adjusted weights sum: {gc.adjusted_gauge_weight_sum()}")

    # Check initial emissions
    initial_emissions1 = gc.weighted_emissions_per_gauge(gauge1.address)
    initial_emissions2 = gc.weighted_emissions_per_gauge(gauge2.address)
    print(f"Initial emissions: gauge1={initial_emissions1}, gauge2={initial_emissions2}")

    # Skip time 30 days
    boa.env.time_travel(30 * 86400)

    # Check weights after 30 days
    weight1_after_30d = gc.get_gauge_weight(gauge1.address)
    weight2_after_30d = gc.get_gauge_weight(gauge2.address)
    print(f"Weights after 30 days: gauge1={weight1_after_30d}, gauge2={weight2_after_30d}")

    # Simulate flashloan operations
    if use_flashloan:
        with boa.env.prank(user):
            # Simulate large deposit (taken from flashloan)
            lp_token1._mint_for_testing(user, 50000000 * 10**18)
            print(gc.adjusted_gauge_weight(gauge1.address), gauge1.get_adjustment(), gc.gauge_weight(gauge1.address))
            gauge1.deposit(50000000 * 10**18, user)
            print(gc.adjusted_gauge_weight(gauge1.address), gauge1.get_adjustment(), gc.gauge_weight(gauge1.address))
            # Return large deposit (taken from flashloan)
            gauge1.withdraw(50000000 * 10**18, user, user)
            print(gc.adjusted_gauge_weight(gauge1.address), gauge1.get_adjustment(), gc.gauge_weight(gauge1.address))
            lp_token1._burn_for_testing(user, 50000000 * 10**18)

    else:
        # Call checkpoints again
        with boa.env.prank(admin):
            gc.checkpoint(gauge1.address)
            gc.checkpoint(gauge2.address)

    # Check weights and emissions after vote change
    mid_weight1 = gc.get_gauge_weight(gauge1.address)
    mid_weight2 = gc.get_gauge_weight(gauge2.address)
    mid_emissions1 = gc.weighted_emissions_per_gauge(gauge1.address)
    mid_emissions2 = gc.weighted_emissions_per_gauge(gauge2.address)
    print(f"Mid weights: gauge1={mid_weight1}, gauge2={mid_weight2}")
    print(f"Mid adjusted weights: gauge1={gc.adjusted_gauge_weight(gauge1.address)}, gauge2={gc.adjusted_gauge_weight(gauge2.address)}")
    print(f"Mid adjusted weights sum: {gc.adjusted_gauge_weight_sum()}")
    print(f"Mid emissions: gauge1={mid_emissions1}, gauge2={mid_emissions2}")

    # Skip time another 30 days
    boa.env.time_travel(1 * 86400)

    # Call checkpoints again
    with boa.env.prank(admin):
        gc.checkpoint(gauge1.address)
        gc.checkpoint(gauge2.address)

    # Check final weights and emissions
    final_weight1 = gc.get_gauge_weight(gauge1.address)
    final_weight2 = gc.get_gauge_weight(gauge2.address)
    final_emissions1 = gc.weighted_emissions_per_gauge(gauge1.address)
    final_emissions2 = gc.weighted_emissions_per_gauge(gauge2.address)
    print(f"Final weights: gauge1={final_weight1}, gauge2={final_weight2}")
    print(f"Final adjusted weights: gauge1={gc.adjusted_gauge_weight(gauge1.address)}, gauge2={gc.adjusted_gauge_weight(gauge2.address)}")
    print(f"Final adjusted weights sum: {gc.adjusted_gauge_weight_sum()}")
    print(f"Final emissions: gauge1={final_emissions1}, gauge2={final_emissions2}")


def test_weight_manipulation(ve_yb, yb, gc, admin, collateral_token, stablecoin):
    # Add new gauges to the GaugeController
    with boa.env.prank(admin):
        dummy_factory = boa.load('contracts/testing/DummyFactoryForGauge.vy', admin, gc.address)
    with boa.env.prank(dummy_factory.address):
        btc_gauge = boa.load('contracts/dao/LiquidityGauge.vy', collateral_token.address)
        usdc_gauge = boa.load('contracts/dao/LiquidityGauge.vy', stablecoin.address)
    with boa.env.prank(admin):
        gc.add_gauge(btc_gauge.address)
        gc.add_gauge(usdc_gauge.address)

    # Regular user gets 1 YB token, locks them, and deposits 5 USDC (50% of supply)
    REGULAR_USER = boa.env.generate_address()
    with boa.env.prank(admin):
        yb.mint(REGULAR_USER, 10 ** 18)
    stablecoin._mint_for_testing(REGULAR_USER, 10 * 10 ** 18)

    print("stablecoin total supply", stablecoin.totalSupply())

    with boa.env.prank(REGULAR_USER):
        yb.approve(ve_yb.address, 2 ** 256 - 1)
        ve_yb.create_lock(10 ** 18, boa.env.evm.patch.timestamp + MAX_TIME)
        ve_yb.infinite_lock_toggle()
        stablecoin.approve(usdc_gauge.address, 2 ** 256 - 1)
        usdc_gauge.deposit(5 * 10 ** 18, REGULAR_USER)
        gc.vote_for_gauge_weights([usdc_gauge.address], [10000])

    # Hacker creates N accounts
    HACKER_ACCOUNTS_CNT = 100
    HACKER_ACCOUNTS = [boa.env.generate_address() for _ in range(HACKER_ACCOUNTS_CNT)]

    # Hacker distributes ~1 YB token between accounts and create locks for all of them
    for i in range(HACKER_ACCOUNTS_CNT):
        size = 10 ** 18 - (HACKER_ACCOUNTS_CNT - 1) if i == 0 else MAX_TIME
        with boa.env.prank(admin):
            yb.mint(HACKER_ACCOUNTS[i], size)
        with boa.env.prank(HACKER_ACCOUNTS[i]):
            yb.approve(ve_yb.address, 2 ** 256 - 1)
            ve_yb.create_lock(size, boa.env.evm.patch.timestamp + MAX_TIME)
            ve_yb.infinite_lock_toggle()

    # Hacker deposits 5 BTC (50% of supply)
    collateral_token._mint_for_testing(HACKER_ACCOUNTS[0], 10 * 10 ** 18)

    print("collateral token total supply", collateral_token.totalSupply())

    with boa.env.prank(HACKER_ACCOUNTS[0]):
        collateral_token.approve(btc_gauge.address, 2 ** 256 - 1)
        btc_gauge.deposit(5 * 10 ** 18, HACKER_ACCOUNTS[0])

    # Voting for the gauge from all hacker accounts, transferring locked tokens
    for i in range(HACKER_ACCOUNTS_CNT):
        with boa.env.prank(HACKER_ACCOUNTS[i]):
            gc.vote_for_gauge_weights([btc_gauge.address], [0])
            if i < HACKER_ACCOUNTS_CNT - 1:
                ve_yb.transferFrom(HACKER_ACCOUNTS[i], HACKER_ACCOUNTS[i + 1], ve_yb.tokenOfOwnerByIndex(HACKER_ACCOUNTS[i], 0))

    # Hacked gauge gets 100x more weight
    btc_gauge_weight = gc.get_gauge_weight(btc_gauge.address)    # 100000000000000004950
    usdc_gauge_weight = gc.get_gauge_weight(usdc_gauge.address)  # 1000000000000000000

    print("btc_gauge_weight", btc_gauge_weight)
    print("usdc_gauge_weight", usdc_gauge_weight)

    boa.env.time_travel(4 * WEEK)

    with boa.env.prank(HACKER_ACCOUNTS[0]):
        claimed_by_hacker = btc_gauge.claim()    # 13336055934441218818416799
    with boa.env.prank(REGULAR_USER):
        claimed_by_regular = usdc_gauge.claim()  # 133360559344412181583157

    print("claimed_by_hacker", claimed_by_hacker)
    print("claimed_by_regular", claimed_by_regular)

    assert claimed_by_regular > 0
    assert claimed_by_hacker == 0
