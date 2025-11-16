import boa
import pytest
from hypothesis import given, settings
import hypothesis.strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule


WEEK = 7 * 86400


@pytest.fixture(scope="session")
def token_set(token_mock):
    decimals = [8] * 2 + [18] * 8
    return [token_mock.deploy("Token %s" % i, "TOK-%s" % i, decimals[i]) for i in range(10)]


@pytest.fixture(scope="session")
def fee_distributor(token_set, ve_yb, admin):
    initial_set = [token_set[0], token_set[1], token_set[5], token_set[9]]
    return boa.load('contracts/dao/FeeDistributor.vy', initial_set, ve_yb, admin)


@given(token_ids=st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=9))
@settings(max_examples=50)
def test_add_set(fee_distributor, token_set, token_ids, admin):
    token_set = [token_set[i] for i in list(set(token_ids))]
    with boa.reverts():
        fee_distributor.add_token_set(token_set)
    with boa.env.prank(admin):
        fee_distributor.add_token_set(token_set)
    assert fee_distributor.current_token_set() == 2
    for i, token in enumerate(token_set):
        assert fee_distributor.token_sets(2, i) == token.address


def test_recover(fee_distributor, token_set, admin):
    amounts = [10**18 + i * 10**18 for i in range(len(token_set))]
    for token, amount in zip(token_set, amounts):
        token._mint_for_testing(fee_distributor.address, amount)

    for token, amount in zip(token_set, amounts):
        with boa.reverts():
            fee_distributor.recover_token(token.address, admin)
        with boa.env.prank(admin):
            fee_distributor.recover_token(token.address, admin)
            assert token.balanceOf(admin) == amount


@given(
        amounts=st.lists(st.integers(min_value=0, max_value=10**30), min_size=10, max_size=10),
        epoch_count=st.integers(min_value=1, max_value=51)
)
@settings(max_examples=50)
def test_claim_empty(fee_distributor, token_set, accounts, amounts, epoch_count):
    for token, amount in zip(token_set, amounts):
        token._mint_for_testing(fee_distributor.address, amount)
    fee_distributor.claim(accounts[0], epoch_count)


@given(
        amounts=st.lists(st.integers(min_value=0, max_value=10**30), min_size=4, max_size=4)
)
@settings(max_examples=500)
def test_claim_two_users(fee_distributor, token_set, accounts, admin, amounts, ve_yb, yb):
    used_set = [token_set[0], token_set[1], token_set[5], token_set[9]]
    users = accounts[:2]
    ve_amounts = [10**18, 3 * 10**18]
    lock_time = boa.env.evm.patch.timestamp + 4 * 365 * 86400

    for user, ve_amount in zip(users, ve_amounts):
        with boa.env.prank(admin):
            yb.mint(user, ve_amount)
        with boa.env.prank(user):
            yb.approve(ve_yb.address, 2**256 - 1)
            ve_yb.create_lock(ve_amount, lock_time)

    for token, amount in zip(used_set, amounts):
        token._mint_for_testing(fee_distributor.address, amount)

    fee_distributor.fill_epochs()

    # 5 weeks claims everything distributed
    boa.env.time_travel(5 * WEEK)

    for user, ve_amount in zip(users, ve_amounts):
        with boa.env.prank(user):
            fee_distributor.claim()
        for token, amount in zip(used_set, amounts):
            user_has = token.balanceOf(user)
            user_expected = amount * ve_amount // sum(ve_amounts)
            assert abs(user_has - user_expected) <= max(user_expected * 1e-6, 8)

    for token in used_set:
        assert token.balanceOf(fee_distributor.address) <= 8


class StatefulFeeDistributor(RuleBasedStateMachine):
    # Stateful test:
    # * start with 1 locked user
    # * add locks
    # * add new sets (no more often than once a week)
    # * distribute coins only in the current set
    # * fill_epochs
    # * time travel
    # * time travel and check that almost everything is used up at teardown
    # TODO: preivew_claim method!

    ve_amount = st.integers(min_value=4 * 365 * 86400, max_value=10**9 * 10**18)
    lock_duration = st.integers(min_value=WEEK, max_value=4 * 365 * 86400)
    user_id = st.integers(min_value=0, max_value=9)
    dt = st.integers(min_value=1, max_value=WEEK)
    epoch_count = st.integers(min_value=-1, max_value=51)
    set_ids = st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=10)
    token_amounts = st.lists(st.integers(min_value=0, max_value=10**30), min_size=10, max_size=10)

    def __init__(self):
        super().__init__()
        self.current_set = set([self.token_set[0], self.token_set[1], self.token_set[5], self.token_set[9]])
        for user in self.accounts:
            with boa.env.prank(user):
                self.yb.approve(self.ve_yb.address, 2**256 - 1)
        with boa.env.prank(self.admin):
            self.yb.mint(self.accounts[0], 10**18)
        with boa.env.prank(self.accounts[0]):
            self.ve_yb.create_lock(10**18, boa.env.evm.patch.timestamp + 4 * 365 * 86400)
        self.tokens_distributed = {t: 0 for t in self.token_set}

    @rule(uid=user_id, amount=ve_amount, duration=lock_duration)
    def create_lock(self, uid, amount, duration):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp + duration
        if self.ve_yb.locked(user).amount == 0:
            with boa.env.prank(self.admin):
                self.yb.mint(user, amount)
            with boa.env.prank(user):
                self.ve_yb.create_lock(amount, t)

    @rule(uid=user_id, duration=lock_duration)
    def extend_lock(self, uid, duration):
        user = self.accounts[uid]
        t0 = boa.env.evm.patch.timestamp
        t = t0 + duration
        locked = self.ve_yb.locked(user)
        if locked.amount > 0 and locked.end > t0 and t // WEEK * WEEK > locked.end:
            with boa.env.prank(user):
                self.ve_yb.increase_unlock_time(t)

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)

    @rule()
    def fill_epochs(self):
        self.fee_distributor.fill_epochs()

    @rule(set_ids=set_ids)
    def add_set(self, set_ids):
        token_set = list(set([self.token_set[i] for i in set_ids]))
        ts_id = self.fee_distributor.current_token_set()
        with boa.env.prank(self.admin):
            self.fee_distributor.add_token_set(token_set)
        ts_id += 1
        assert self.fee_distributor.current_token_set() == ts_id
        for i, token in enumerate(token_set):
            assert self.fee_distributor.token_sets(ts_id, i) == token.address
        self.current_set = set(token_set)

    @rule(uid=user_id, epoch_count=epoch_count)
    def claim(self, uid, epoch_count):
        user = self.accounts[uid]
        if epoch_count < 0:
            self.fee_distributor.claim(user)
        elif epoch_count == 0:
            with boa.reverts():
                self.fee_distributor.claim(user, 0)
        else:
            self.fee_distributor.claim(user, epoch_count)

    @rule(amounts=token_amounts)
    def distribute(self, amounts):
        for token, amount in zip(self.token_set, amounts):
            if amount > 0 and token in self.current_set:
                token._mint_for_testing(self.fee_distributor, amount)

    def teardown(self):
        self.fee_distributor.fill_epochs()
        boa.env.time_travel(5 * WEEK)
        t_w = boa.env.evm.patch.timestamp // WEEK * WEEK
        for user in self.accounts:
            while self.fee_distributor.last_claimed_for(user) < t_w:
                with boa.env.prank(user):
                    self.fee_distributor.claim()
        for token in self.token_set:
            assert token.balanceOf(self.fee_distributor.address) <= 8 * 100
        super().teardown()


def test_st_fee_distributor(fee_distributor, token_set, accounts, admin, ve_yb, yb):
    StatefulFeeDistributor.TestCase.settings = settings(max_examples=200, stateful_step_count=100)
    for k, v in locals().items():
        setattr(StatefulFeeDistributor, k, v)
    run_state_machine_as_test(StatefulFeeDistributor)


def test_st_fee_distributor_too_much_left(fee_distributor, token_set, accounts, admin, ve_yb, yb):
    StatefulFeeDistributor.TestCase.settings = settings(max_examples=200, stateful_step_count=100)
    for k, v in locals().items():
        setattr(StatefulFeeDistributor, k, v)
    state = StatefulFeeDistributor()
    state.distribute(amounts=[0, 0, 0, 0, 0, 0, 0, 0, 0, 10**18])
    state.teardown()
