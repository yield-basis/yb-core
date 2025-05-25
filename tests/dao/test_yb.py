import boa
from math import exp
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule
from .conftest import RATE


def test_mint(yb, admin, accounts):
    assert yb.totalSupply() == 0
    with boa.reverts():
        with boa.env.prank(accounts[1]):
            yb.mint(accounts[0], 10**18)

    with boa.env.prank(admin):
        yb.mint(accounts[0], 10**18)
        assert yb.totalSupply() == 10**18
        assert yb.balanceOf(accounts[0]) == 10**18

        yb.renounce_ownership()
        assert yb.is_minter(admin) is False

        with boa.reverts():
            yb.mint(accounts[0], 10**17)


class StatefulYB(RuleBasedStateMachine):
    dt = st.integers(min_value=0, max_value=30 * 86400)
    rate_factor = st.integers(min_value=0, max_value=2 * 10**18)

    # XXX preallocation in init?

    @rule(dt=dt, rate_factor=rate_factor)
    def emit(self, dt, rate_factor):
        user = self.accounts[0]
        t0 = boa.env.evm.patch.timestamp
        boa.env.time_travel(dt)
        t = boa.env.evm.patch.timestamp
        if rate_factor > 10**18:
            with boa.reverts():
                self.yb.preview_emissions(t, rate_factor)
        else:
            rate = RATE * rate_factor // 10**18
            expected_emissions = self.yb.preview_emissions(t, rate_factor)
            reserve = d_reserve = self.yb.reserve()
            calculated_emissions = int(reserve * (1 - exp(-(t - t0) * rate / 1e18)))
            d_balance = self.yb.balanceOf(user)
            with boa.env.prank(self.admin):
                emitted = self.yb.emit(user, rate_factor)
            d_balance = self.yb.balanceOf(user) - d_balance
            d_reserve -= self.yb.reserve()
            assert d_balance == emitted == expected_emissions
            assert abs(emitted - calculated_emissions) / (emitted + 1) < 1e-9


def test_yb(yb, admin, accounts):
    StatefulYB.TestCase.settings = settings(max_examples=200, stateful_step_count=100)
    for k, v in locals().items():
        setattr(StatefulYB, k, v)
    run_state_machine_as_test(StatefulYB)
