import boa
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, initialize


VEST_SIZE = 10**8 * 10**18


class StatefulVest(RuleBasedStateMachine):
    preallocation = st.lists(st.integers(min_value=10**18, max_value=10**8 * 10**18), min_size=10, max_size=10)
    time_delay = st.integers(min_value=0, max_value=2*365*86400)
    account = st.integers(min_value=0, max_value=9)

    @initialize(preallocation=preallocation, dt_start=time_delay, dt_end=time_delay, dt_cliff=time_delay)
    def preallocate(self, preallocation, dt_start, dt_end, dt_cliff):
        psum = sum(preallocation)
        preallocation = [p * VEST_SIZE // psum for p in preallocation]
        self.preallocation = preallocation

        t0 = boa.env.evm.patch.timestamp
        self.t_start = t0 + dt_start
        self.t_end = self.t_start + dt_end + 1
        self.t_cliff = self.t_start + dt_cliff + 1  # This will definitely create CliffEscrows

        with boa.env.prank(self.admin):
            self.vest_factory = self.vest_impl.deploy(
                    self.yb.address, self.t_start, self.t_end, True, self.cliff_factory.address)
            self.yb.approve(self.vest_factory.address, 2**256 - 1)
            self.vest_factory.add_tokens(VEST_SIZE)
            self.vest_factory.fund(self.accounts, preallocation, self.t_cliff)

    @rule(uid=account)
    def claim(self, uid):
        ce = self.vest_factory.recipient_to_cliff(self.accounts[uid])
        if self.vest_factory.disabled_at(ce) == 0:
            vested = self.vest_factory.vestedOf(ce)
            locked = self.vest_factory.lockedOf(ce)
            claimed = self.vest_factory.total_claimed(ce)
            assert vested + locked == self.preallocation[uid]
            balance_before = self.yb.balanceOf(ce)
            self.vest_factory.claim(ce)
            balance_after = self.yb.balanceOf(ce)
            new_claimed = self.vest_factory.total_claimed(ce)
            assert new_claimed == balance_after - balance_before + claimed
            if boa.env.evm.patch.timestamp >= self.t_end:
                assert new_claimed == self.preallocation[uid]

    @rule(uid=account)
    def toggle_disable(self, uid):
        ce = self.vest_factory.recipient_to_cliff(self.accounts[uid])
        with boa.env.prank(self.admin):
            if not self.vest_factory.disabled_rugged(ce):
                self.vest_factory.toggle_disable(ce)
            else:
                with boa.reverts():
                    self.vest_factory.toggle_disable(ce)

    @rule(uid=account)
    def rug(self, uid):
        ce = self.vest_factory.recipient_to_cliff(self.accounts[uid])
        with boa.env.prank(self.admin):
            if self.vest_factory.disabled_rugged(ce) or self.vest_factory.disabled_at(ce) == 0:
                with boa.reverts():
                    self.vest_factory.rug_disabled(ce, self.admin)
            else:
                b = self.vest_factory.lockedOf(ce)
                admin_before = self.yb.balanceOf(self.admin)
                self.vest_factory.rug_disabled(ce, self.admin)
                assert self.yb.balanceOf(self.admin) - admin_before == b

    @rule(owner=account, caller=account, recipient=account)
    def claim_cliff(self, owner, caller, recipient):
        owner = self.accounts[owner]
        caller = self.accounts[caller]
        recipient = self.accounts[recipient]
        ce = self.cliff_impl.at(self.vest_factory.recipient_to_cliff(owner))
        amount = self.yb.balanceOf(ce.address)
        if amount > 0:
            transfer_allowed = (boa.env.evm.patch.timestamp >= self.t_cliff) and (caller == owner or recipient == owner)
            with boa.env.prank(caller):
                if transfer_allowed:
                    before = self.yb.balanceOf(recipient)
                    ce.transfer(recipient, amount)
                    after = self.yb.balanceOf(recipient)
                    assert amount == after - before
                else:
                    with boa.reverts():
                        ce.transfer(recipient, amount)

    @rule(owner=account, dt=time_delay)
    def create_lock(self, owner, dt):
        owner = self.accounts[owner]
        ce = self.cliff_impl.at(self.vest_factory.recipient_to_cliff(owner))
        amount = self.yb.balanceOf(ce.address)

        if amount > 0 and dt > 0 and self.ve_yb.locked(ce.address).amount == 0:
            unlock_time = boa.env.evm.patch.timestamp + dt + 7 * 86400
            if self.accounts[0] != owner:
                with boa.reverts():
                    ce.create_lock(amount, unlock_time)
            with boa.env.prank(owner):
                ce.create_lock(amount, unlock_time)

    @rule(dt=time_delay)
    def time_travel(self, dt):
        boa.env.time_travel(dt)


def test_vest(mock_gov_token, yb, ve_yb, gc, admin, accounts):
    StatefulVest.TestCase.settings = settings(max_examples=200, stateful_step_count=100)

    gauge = boa.load('contracts/testing/MockLiquidityGauge.vy', mock_gov_token.address)
    with boa.env.prank(admin):
        gc.add_gauge(gauge.address)
        yb.mint(admin, VEST_SIZE)

    cliff_impl = boa.load_partial('contracts/dao/CliffEscrow.vy')
    cliff_factory = cliff_impl.deploy(yb.address, ve_yb.address, gc.address)
    vest_impl = boa.load_partial('contracts/dao/VestingEscrow.vy')

    for k, v in locals().items():
        setattr(StatefulVest, k, v)

    run_state_machine_as_test(StatefulVest)

