import boa
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, invariant


WEEK = 7 * 86400
MAX_TIME = 86400 * 365 * 4


def test_ve_admin(ve_mock, admin, accounts):
    assert ve_mock.owner() == admin
    with boa.env.prank(admin):
        ve_mock.transfer_ownership(accounts[0])
    assert ve_mock.owner() == accounts[0]


class StatefulVE(RuleBasedStateMachine):
    USER_TOTAL = 10**40
    user_id = st.integers(min_value=0, max_value=9)
    amount = st.integers(min_value=0, max_value=(2**256 - 1))
    lock_duration = st.integers(min_value=0, max_value=(2**256 - 1))
    dt = st.integers(min_value=0, max_value=30 * 86400)

    def __init__(self):
        super().__init__()
        self.voting_balances = {}
        for user in self.accounts:
            with boa.env.prank(user):
                self.mock_gov_token.approve(self.ve_mock.address, 2**256 - 1)
            self.mock_gov_token._mint_for_testing(user, self.USER_TOTAL)
            self.voting_balances[user] = {'value': 0, 'unlock_time': 0}

        self.initial_timestamp = boa.env.evm.patch.timestamp

    @rule(uid=user_id, amount=amount, lock_duration=lock_duration)
    def create_lock(self, uid, amount, lock_duration):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        unlock_time = min(t + lock_duration, 2**256 - 1)
        unlock_time_round = unlock_time // WEEK * WEEK
        with boa.env.prank(user):
            if amount < MAX_TIME:
                with boa.reverts():
                    self.ve_mock.create_lock(amount, unlock_time)
            elif self.voting_balances[user]['value'] > 0:
                with boa.reverts('Withdraw old tokens first'):
                    self.ve_mock.create_lock(amount, unlock_time)
            elif unlock_time_round <= t:
                with boa.reverts('Can only lock until time in the future'):
                    self.ve_mock.create_lock(amount, unlock_time)
            elif unlock_time_round > t + MAX_TIME:
                with boa.reverts('Voting lock can be 4 years max'):
                    self.ve_mock.create_lock(amount, unlock_time)
            elif amount > self.mock_gov_token.balanceOf(user):
                with boa.reverts():
                    self.ve_mock.create_lock(amount, unlock_time)
            else:
                self.ve_mock.create_lock(amount, unlock_time)
                self.voting_balances[user] = {'value': amount // MAX_TIME * MAX_TIME, 'unlock_time': unlock_time_round}
                st_amount, st_time = self.ve_mock.locked(user)
                assert st_amount == amount // MAX_TIME * MAX_TIME
                assert st_time == unlock_time_round

    @rule(uid=user_id, amount=amount)
    def increase_amount(self, uid, amount):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        with boa.env.prank(user):
            if amount < MAX_TIME:
                with boa.reverts():
                    self.ve_mock.increase_amount(amount)
            elif self.voting_balances[user]['value'] == 0:
                with boa.reverts('No existing lock found'):
                    self.ve_mock.increase_amount(amount)
            elif self.voting_balances[user]['unlock_time'] <= t:
                with boa.reverts('Cannot add to expired lock. Withdraw'):
                    self.ve_mock.increase_amount(amount)
            elif amount > self.mock_gov_token.balanceOf(user):
                with boa.reverts():
                    self.ve_mock.increase_amount(amount)
            else:
                self.ve_mock.increase_amount(amount)
                self.voting_balances[user]['value'] += amount // MAX_TIME * MAX_TIME

    @rule(uid=user_id, lock_duration=lock_duration)
    def increase_unlock_time(self, uid, lock_duration):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        unlock_time = min(t + lock_duration, 2**256 - 1)
        unlock_time_round = unlock_time // WEEK * WEEK
        with boa.env.prank(user):
            if self.voting_balances[user]['unlock_time'] <= t:
                if self.voting_balances[user]['unlock_time'] == 0:
                    with boa.reverts():
                        self.ve_mock.increase_unlock_time(unlock_time)
                else:
                    with boa.reverts('Lock expired'):
                        self.ve_mock.increase_unlock_time(unlock_time)
            elif self.voting_balances[user]['value'] == 0:
                with boa.reverts('Nothing is locked'):
                    self.ve_mock.increase_unlock_time(unlock_time)
            elif unlock_time_round <= self.voting_balances[user]['unlock_time']:
                with boa.reverts('Can only increase lock duration'):
                    self.ve_mock.increase_unlock_time(unlock_time)
            elif unlock_time_round > t + MAX_TIME:
                with boa.reverts('Voting lock can be 4 years max'):
                    self.ve_mock.increase_unlock_time(unlock_time)
            else:
                self.ve_mock.increase_unlock_time(unlock_time)
                self.voting_balances[user]['unlock_time'] = self.ve_mock.locked(user).end

    @rule(uid=user_id)
    def infinite_lock_toggle(self, uid):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        with boa.env.prank(user):
            if self.voting_balances[user]['unlock_time'] <= t:
                with boa.reverts('Lock expired'):
                    self.ve_mock.infinite_lock_toggle()
            elif self.voting_balances[user]['value'] == 0:
                with boa.reverts('Nothing is locked'):
                    self.ve_mock.infinite_lock_toggle()
            else:
                self.ve_mock.infinite_lock_toggle()
                self.voting_balances[user]['unlock_time'] = self.ve_mock.locked(user).end

    @rule(uid=user_id)
    def withdraw(self, uid):
        user = self.accounts[uid]
        t = boa.env.evm.patch.timestamp
        with boa.env.prank(user):
            if self.voting_balances[user]['unlock_time'] > t:
                with boa.reverts("The lock didn't expire"):
                    self.ve_mock.withdraw()
            elif self.voting_balances[user]['value'] == 0:
                with boa.reverts('erc721: invalid token ID'):
                    self.ve_mock.withdraw()
            else:
                self.ve_mock.withdraw()
                self.voting_balances[user]['value'] = 0

    @rule(uid1=user_id, uid2=user_id)
    def merge(self, uid1, uid2):
        user1 = self.accounts[uid1]
        user2 = self.accounts[uid2]
        max_time = boa.env.evm.patch.timestamp // WEEK * WEEK + MAX_TIME
        t1 = self.voting_balances[user1]['unlock_time']
        t2 = self.voting_balances[user2]['unlock_time']
        if self.voting_balances[user1]['value'] == 0:
            with boa.reverts():
                self.ve_mock.tokenOfOwnerByIndex(user1, 0)
            return
        id1 = self.ve_mock.tokenOfOwnerByIndex(user1, 0)
        with boa.env.prank(user1):
            if user1 == user2:
                with boa.reverts():
                    self.ve_mock.transferFrom(user1, user2, id1)
            elif self.voting_balances[user2]['value'] == 0:
                with boa.reverts():
                    self.ve_mock.transferFrom(user1, user2, id1)
            elif (t1 != max_time and t1 != 2**256 - 1) or (t2 != max_time and t2 != 2**256 - 1) or\
                    (t1 // WEEK * WEEK != t2 // WEEK * WEEK):
                with boa.reverts("Need max veLock"):
                    self.ve_mock.transferFrom(user1, user2, id1)
            else:
                self.ve_mock.transferFrom(user1, user2, id1)
                self.voting_balances[user2]['value'] += self.voting_balances[user1]['value']
                self.voting_balances[user1]['value'] = 0

    @rule(uid=user_id)
    def checkpoint(self, uid):
        with boa.env.prank(self.accounts[uid]):
            self.ve_mock.checkpoint()

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)

    @invariant()
    def token_balances(self):
        for user in self.accounts:
            assert self.mock_gov_token.balanceOf(user) == 10**40 - self.voting_balances[user]['value']

    @invariant()
    def escrow_current_votes(self):
        total_votes = 0
        timestamp = boa.env.evm.patch.timestamp
        for acct in self.accounts:
            data = self.voting_balances[acct]
            vote = self.ve_mock.getVotes(acct)
            total_votes += vote
            if data["unlock_time"] > timestamp and data["value"] // MAX_TIME > 0:
                assert vote > 0
            elif data["value"] == 0 or data["unlock_time"] <= timestamp:
                assert vote == 0
        assert self.ve_mock.totalVotes() == total_votes

    @rule(dt=dt)
    def historic_votes(self, dt):
        timestamp = max(boa.env.evm.patch.timestamp - dt, self.initial_timestamp)
        total_votes = sum(self.ve_mock.getPastVotes(user, timestamp) for user in self.accounts)
        assert self.ve_mock.getPastTotalSupply(timestamp) == total_votes

    @invariant()
    def check_vote_decay(self):
        now = boa.env.evm.patch.timestamp
        for user in self.accounts:
            user_votes = self.ve_mock.getVotes(user)
            if self.voting_balances[user]['unlock_time'] == 2**256 - 1:
                expected_votes = self.voting_balances[user]['value']
            else:
                expected_votes = max(
                    self.voting_balances[user]['value'] // MAX_TIME * (self.voting_balances[user]['unlock_time'] - now),
                    0)
            assert abs(user_votes - expected_votes) <= 10


def test_ve(ve_mock, mock_gov_token, accounts):
    StatefulVE.TestCase.settings = settings(max_examples=200, stateful_step_count=100)  # 2000, 100
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    run_state_machine_as_test(StatefulVE)


def test_nothing_is_locked(ve_mock, mock_gov_token, accounts):
    StatefulVE.TestCase.settings = settings(max_examples=200, stateful_step_count=100)  # 2000, 100
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    state = StatefulVE()
    state.check_vote_decay()
    state.escrow_current_votes()
    state.token_balances()
    state.increase_unlock_time(lock_duration=0, uid=0)
    state.teardown()


def test_merge_votes(yb, ve_yb, accounts, admin):
    user1 = accounts[0]
    user2 = accounts[1]

    amount1 = 1 * 10**18
    print(f"amount1: {amount1}")

    amount2 = 2 * 10**18
    print(f"amount2: {amount2}")

    lock_time = boa.env.evm.patch.timestamp + 4 * 365 * 86400

    with boa.env.prank(admin):
        yb.mint(user1, amount1)
        yb.mint(user2, amount2)

    with boa.env.prank(user1):
        yb.approve(ve_yb.address, amount1)
        ve_yb.create_lock(amount1, lock_time)

    with boa.env.prank(user2):
        yb.approve(ve_yb.address, amount2)
        ve_yb.create_lock(amount2, lock_time)

    votes1 = ve_yb.getVotes(user1)
    votes2 = ve_yb.getVotes(user2)
    total_votes = ve_yb.totalVotes()

    slope1 = ve_yb.get_last_user_point(user1)[1]
    slope2 = ve_yb.get_last_user_point(user2)[1]
    global_slope = ve_yb.point_history(ve_yb.epoch())[1]

    print("votes1", votes1)
    print("votes2", votes2)
    print("total_votes", total_votes)

    print("slope1", slope1)
    print("slope2", slope2)
    print("global_slope", global_slope)

    assert votes1 > 0
    assert votes2 > 0
    assert total_votes == votes1 + votes2

    # Merge votes: user1 transfers to user2
    print("\nMERGE\n")
    token_id = ve_yb.tokenOfOwnerByIndex(user1, 0)

    with boa.env.prank(user1):
        ve_yb.transferFrom(user1, user2, token_id)

    votes1_merged = ve_yb.getVotes(user1)
    votes2_merged = ve_yb.getVotes(user2)
    total_votes_merged = ve_yb.totalVotes()

    slope1_merged = ve_yb.get_last_user_point(user1)[1]
    slope2_merged = ve_yb.get_last_user_point(user2)[1]
    global_slope_merged = ve_yb.point_history(ve_yb.epoch())[1]

    print("slope1_merged", slope1_merged)
    print("slope2_merged", slope2_merged)
    print("global_slope_merged", global_slope_merged)

    print("votes1_merged", votes1_merged)
    print("votes2_merged", votes2_merged)
    print("total_votes_merged", total_votes_merged)

    assert votes1_merged == 0
    assert votes2_merged == votes1 + votes2  # assert 2988928724211579825 == (996309574653407625 + 1992619149432493725)
    assert votes2_merged == total_votes_merged
    assert total_votes_merged == total_votes

    # weigth_of_user <= total_weight

    # Problem
    # (a + b) // MAXTIME != (a // MAXTIME) + (b // MAXTIME)
    #    ^                   ^                ^
    #    |                   |                |
    # slope2_merged        slope1           slope2

    # => condition for such situation:
    # (amount1 % MAXTIME) + (amount2 % MAXTIME) >= MAXTIME
