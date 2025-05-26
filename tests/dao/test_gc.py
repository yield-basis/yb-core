import boa
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule


WEEK = 7 * 86400
MAX_TIME = 86400 * 365 * 4


class StatefulVE(RuleBasedStateMachine):
    USER_TOTAL = 10**40
    user_id = st.integers(min_value=0, max_value=9)
    amount = st.integers(min_value=1, max_value=10**40)
    lock_duration = st.integers(min_value=7 * 86400, max_value=MAX_TIME)
    dt = st.integers(min_value=0, max_value=30 * 86400)

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

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)


def test_ve(ve_yb, yb, accounts, admin):
    StatefulVE.TestCase.settings = settings(max_examples=200, stateful_step_count=100)  # 2000, 100
    for k, v in locals().items():
        setattr(StatefulVE, k, v)
    run_state_machine_as_test(StatefulVE)
