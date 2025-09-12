import boa
import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, initialize


VEST_SIZE = 10**8 * 10**18


class StatefulVest(RuleBasedStateMachine):
    preallocation = st.lists(st.integers(min_value=10**18, max_value=10**8 * 10**18), min_size=10, max_size=10)
    time_delay = st.integers(min_value=0, max_value=2*365*86400)

    @initialize(preallocation=preallocation, dt_start=time_delay, dt_end=time_delay, dt_cliff=time_delay)
    def preallocate(self, preallocation, dt_start, dt_end, dt_cliff):
        psum = sum(preallocation)
        preallocation = [p * VEST_SIZE // psum for p in preallocation]

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

    @rule()
    def dummy(self):
        pass


def test_vest(mock_gov_token, yb, ve_yb, gc, admin, accounts):
    StatefulVest.TestCase.settings = settings(max_examples=200, stateful_step_count=100)

    gauge = boa.load('contracts/testing/MockLiquidityGauge.vy', mock_gov_token.address)
    with boa.env.prank(admin):
        gc.add_gauge(gauge.address)
        yb.mint(admin, VEST_SIZE)

    cliff_factory = boa.load('contracts/dao/CliffEscrow.vy', yb.address, ve_yb.address, gc.address)
    vest_impl = boa.load_partial('contracts/dao/VestingEscrow.vy')

    for k, v in locals().items():
        setattr(StatefulVest, k, v)

    run_state_machine_as_test(StatefulVest)

