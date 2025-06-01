import pytest
import boa
import os
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

    @rule(from_uid=user_id, to_uid=user_id, amount=token_amount, gid=gauge_id)
    def transfer(self, from_uid, to_uid, amount, gid):
        gauge = self.gauges[gid]
        from_user = self.accounts[from_uid]
        to_user = self.accounts[to_uid]
        if amount <= gauge.balanceOf(from_user):
            with boa.env.prank(from_user):
                gauge.transfer(to_user, amount)

    @rule()
    def claim(self):
        for user in self.accounts:
            with boa.env.prank(user):
                for gauge in self.gauges:
                    expected_amount = gauge.preview_claim(self.yb.address, user)
                    d_yb = self.yb.balanceOf(user)
                    gauge.claim()
                    d_yb = self.yb.balanceOf(user) - d_yb
                    assert expected_amount == d_yb

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

    def mint_all(self):
        for user in self.accounts:
            with boa.env.prank(user):
                for gauge in self.gauges:
                    gauge.claim()

    @rule(dt=dt)
    def check_mint_sum(self, dt):
        self.mint_all()
        t = boa.env.evm.patch.timestamp
        rate_factor = self.gc.adjusted_gauge_weight_sum() * 10**18 // (self.gc.gauge_weight_sum() or 1)
        assert rate_factor <= 10**18
        expected_emissions = self.yb.preview_emissions(t + dt, rate_factor)

        supply_before = self.yb.totalSupply()
        balances_before = [self.yb.balanceOf(user) for user in self.accounts]

        boa.env.time_travel(dt)
        self.mint_all()

        supply_after = self.yb.totalSupply()
        balances_after = [self.yb.balanceOf(user) for user in self.accounts]

        assert supply_before + expected_emissions == supply_after
        assert sum(balances_before) + expected_emissions == sum(balances_after)

    @rule(dt=dt, gid=gauge_id)
    def check_mint_split_between_users(self, gid, dt):
        gauge = self.gauges[gid]
        lp_balances = [gauge.balanceOf(user) for user in self.accounts]

        for user in self.accounts:
            with boa.env.prank(user):
                gauge.claim()

        boa.env.time_travel(dt)

        claimed = []
        for user in self.accounts:
            with boa.env.prank(user):
                claimed.append(gauge.claim())

        if sum(claimed) > 0:
            for claim, lp_balance in zip(claimed, lp_balances):
                assert abs(claim / sum(claimed) - lp_balance / (sum(lp_balances) or 1)) <= 1e-8

    @rule(dt=dt, uid=user_id)
    def check_mint_split_between_gauges(self, uid, dt):
        user = self.accounts[uid]
        lp_fracs = [g.balanceOf(user) / (g.totalSupply() or 1) for g in self.gauges]
        adjustments = [g.get_adjustment() for g in self.gauges]
        avotes = [a * v / 1e18 for a, v in zip(adjustments, VOTES)]

        with boa.env.prank(user):
            for g in self.gauges:
                g.claim()
            supply_before = self.yb.totalSupply()

            boa.env.time_travel(dt)

            claimed = []
            for g in self.gauges:
                claimed.append(g.claim())
            supply_after = self.yb.totalSupply()

            for claim, frac, vote in zip(claimed, lp_fracs, avotes):
                exp_claimed = (supply_after - supply_before) * vote / (sum(avotes) or 1) * frac
                assert abs(claim - exp_claimed) <= max(max(claim, exp_claimed) / 1e6, 1)

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)

    # XXX TODO add_reward etc for non-standard rewards


@pytest.mark.parametrize("_tmp", range(int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", 1))))  # This splits the test into small chunks which are easier to parallelize
def test_gauges(mock_lp, gauges, gc, yb, accounts, vote_for_gauges, _tmp):
    StatefulG.TestCase.settings = settings(max_examples=20, stateful_step_count=100)
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    run_state_machine_as_test(StatefulG)
