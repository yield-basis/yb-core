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
            shares_after = self.gauges[gid].previewDeposit(assets) + self.gauges[gid].totalSupply()
            if shares_after >= 10**12 or shares_after == 0:
                self.gauges[gid].deposit(assets, user)
            else:
                with boa.reverts("Leave MIN_SHARES"):
                    self.gauges[gid].deposit(assets, user)

    @rule(uid=user_id, shares=token_amount, gid=gauge_id)
    def withdraw(self, uid, shares, gid):
        user = self.accounts[uid]
        with boa.env.prank(user):
            if shares <= self.gauges[gid].balanceOf(user):
                remaints = self.gauges[gid].totalSupply() - shares
                if remaints >= 10**12 or remaints == 0:
                    self.gauges[gid].redeem(shares, user, user)
                else:
                    with boa.reverts("Leave MIN_SHARES"):
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
                    self.gc.preview_emissions(gauge.address, boa.env.evm.patch.timestamp)
                    d_yb = self.yb.balanceOf(user)
                    claimed = gauge.claim()
                    d_yb = self.yb.balanceOf(user) - d_yb
                    assert expected_amount == d_yb == claimed

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
        assert abs(sum(balances_before) + expected_emissions - sum(balances_after)) <= len(self.gauges) + len(self.accounts)

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
                assert abs(claim / sum(claimed) - lp_balance / (sum(lp_balances) or 1)) <= max(1e-8, 5 / sum(claimed))

    @rule(dt=dt, uid=user_id)
    def check_mint_split_between_gauges(self, uid, dt):
        user = self.accounts[uid]
        gauge_supplies = [g.totalSupply() for g in self.gauges]
        lp_fracs = [g.balanceOf(user) / (supply or 1) for g, supply in zip(self.gauges, gauge_supplies)]

        with boa.env.prank(user):
            for g in self.gauges:
                g.claim()
            supply_before = self.yb.totalSupply()

            relative_weights = [self.gc.gauge_relative_weight(g) for g in self.gauges]

            boa.env.time_travel(dt)

            claimed = []
            for g in self.gauges:
                claimed.append(g.claim())
            supply_after = self.yb.totalSupply()

            for claim, frac, rw in zip(claimed, lp_fracs, relative_weights):
                exp_claimed = (supply_after - supply_before) * rw / 1e18 * frac
                assert abs(claim - exp_claimed) <= max(max(claim, exp_claimed) / 1e6, 2)

    @rule(dt=dt)
    def time_travel(self, dt):
        boa.env.time_travel(dt)

    # XXX TODO add_reward etc for non-standard rewards


@pytest.mark.parametrize("_tmp", range(int(os.environ.get("PYTEST_XDIST_WORKER_COUNT", 1))))  # This splits the test into small chunks which are easier to parallelize
def test_gauges(mock_lp, gauges, gc, yb, accounts, vote_for_gauges, _tmp):
    StatefulG.TestCase.settings = settings(max_examples=100, stateful_step_count=20)
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    run_state_machine_as_test(StatefulG)


def test_gauges_fail_1(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.deposit(assets=9_301_439_525_721_630_046, gid=3, uid=0)
    state.check_mint_sum(dt=0)
    state.teardown()


def test_gauges_fail_2(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.deposit(assets=9_301_439_525_721_630_046, gid=3, uid=0)
    state.check_mint_sum(dt=700633)
    state.teardown()


def test_gauges_fail_3(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.deposit(assets=10_000_000_000_000_000_000_000_000, gid=0, uid=0)
    state.check_mint_sum(dt=1838538)
    state.check_mint_sum(dt=1147388)
    state.claim()
    state.claim()
    state.check_mint_sum(dt=2053027)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_split_between_users(dt=217458, gid=0)
    state.check_adjustment()
    state.deposit(assets=146, gid=2, uid=2)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1310189, gid=4)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1734980, gid=4)
    state.check_adjustment()
    state.check_mint_sum(dt=1703661)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1071981, uid=6)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_sum(dt=1977912)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_split_between_users(dt=162198, gid=0)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=288073, gid=4)
    state.check_adjustment()
    state.check_mint_sum(dt=2225009)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=890505, uid=2)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=2249571, uid=8)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1119454, gid=1)
    state.check_adjustment()
    state.check_mint_sum(dt=396143)
    state.check_adjustment()
    state.check_mint_sum(dt=440485)
    state.check_adjustment()
    state.check_mint_sum(dt=2324613)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=2315467, uid=5)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=2044035, gid=4)
    state.check_adjustment()
    state.check_mint_sum(dt=485317)
    state.check_adjustment()
    state.deposit(assets=62043, gid=1, uid=3)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.deposit(assets=987_409_287_706_560_566, gid=4, uid=1)
    state.check_adjustment()
    state.deposit(assets=9_072_009_186_415_603_569_180_919, gid=2, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=2297212, uid=6)
    state.check_adjustment()
    state.deposit(assets=19, gid=2, uid=2)
    state.check_adjustment()
    state.check_mint_sum(dt=1117609)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1040859, uid=1)
    state.teardown()


def test_gauges_fail_4(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.deposit(assets=74, gid=1, uid=3)
    state.transfer(amount=14971, from_uid=1, gid=1, to_uid=1)
    state.transfer(amount=3451, from_uid=8, gid=2, to_uid=1)
    state.transfer(amount=3_605_411_359_662_456_793_667_975, from_uid=8, gid=0, to_uid=2)
    state.time_travel(dt=1736184)
    state.transfer(amount=148, from_uid=5, gid=2, to_uid=0)
    state.transfer(amount=222, from_uid=0, gid=2, to_uid=6)
    state.check_mint_sum(dt=1970967)
    state.deposit(assets=62, gid=3, uid=5)
    state.check_mint_sum(dt=24809)
    state.time_travel(dt=2039986)
    state.transfer(amount=28113, from_uid=3, gid=1, to_uid=1)
    state.deposit(assets=3_616_553_094, gid=1, uid=0)
    state.deposit(assets=664_027_503_843_667_059_597_143, gid=3, uid=3)
    state.check_mint_sum(dt=1415274)
    state.time_travel(dt=1483461)
    state.claim()
    state.teardown()


def test_gauges_fail_5(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=0, uid=0)
    state.check_adjustment()
    state.deposit(assets=0, gid=0, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=0, uid=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=0, uid=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=2)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=2)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=2)
    state.check_adjustment()
    state.check_mint_sum(dt=2)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=0, gid=0)
    state.check_adjustment()
    state.deposit(assets=2_313_596_689_383_834_109_234_980, gid=4, uid=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.withdraw(gid=0, shares=0, uid=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.deposit(assets=794, gid=3, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=5, gid=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.time_travel(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.deposit(assets=2_263_314_945, gid=3, uid=4)
    state.check_adjustment()
    state.withdraw(gid=0, shares=0, uid=0)
    state.check_adjustment()
    state.withdraw(gid=0, shares=0, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=116859, gid=3)
    state.teardown()


def test_gauges_fail_6(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.check_adjustment()
    state.time_travel(dt=628508)
    state.check_adjustment()
    state.deposit(assets=40778, gid=2, uid=6)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.deposit(assets=2_740_144_591, gid=2, uid=0)
    state.check_adjustment()
    state.deposit(assets=55977, gid=2, uid=2)
    state.check_adjustment()
    state.deposit(assets=40192, gid=3, uid=3)
    state.check_adjustment()
    state.withdraw(gid=3, shares=10_000_000_000_000_000_000_000_000, uid=9)
    state.check_adjustment()
    state.check_mint_sum(dt=747875)
    state.check_adjustment()
    state.withdraw(gid=3, shares=733236257, uid=2)
    state.check_adjustment()
    state.withdraw(gid=4, shares=42448, uid=9)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.withdraw(gid=2, shares=164, uid=2)
    state.check_adjustment()
    state.deposit(assets=35029, gid=2, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=952444, uid=3)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=422305, gid=4)
    state.check_adjustment()
    state.deposit(assets=16920, gid=3, uid=7)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=643354, uid=9)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1487832, uid=2)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=2058222, gid=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1654823, uid=0)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.transfer(amount=7_468_227_568_673_361_857_602_638, from_uid=4, gid=2, to_uid=5)
    state.check_adjustment()
    state.deposit(assets=126, gid=0, uid=1)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.transfer(amount=61, from_uid=2, gid=4, to_uid=4)
    state.check_adjustment()
    state.time_travel(dt=617145)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1785904, uid=8)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.deposit(assets=0, gid=4, uid=0)
    state.check_adjustment()
    state.deposit(assets=11_902_150_556_549_232_722, gid=3, uid=8)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=132263, uid=8)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.withdraw(gid=1, shares=143, uid=7)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=2592000, gid=1)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1497282, gid=1)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1293335, uid=6)
    state.check_adjustment()
    state.check_mint_sum(dt=1172018)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_sum(dt=381914)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=2421427, uid=4)
    state.check_adjustment()
    state.check_mint_sum(dt=673444)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1983640, gid=4)
    state.check_adjustment()
    state.withdraw(gid=0, shares=48245, uid=2)
    state.check_adjustment()
    state.deposit(assets=58988, gid=0, uid=1)
    state.check_adjustment()
    state.deposit(assets=8_230_964_378_253_163_429_744_791, gid=0, uid=8)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1734288, uid=4)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_sum(dt=2337958)
    state.check_adjustment()
    state.time_travel(dt=1513866)
    state.check_adjustment()
    state.deposit(assets=8_666_142_019_549_455_104_715_444, gid=0, uid=9)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=891667, uid=8)
    state.check_adjustment()
    state.deposit(assets=1_688_517_195_081_464_588_013_232, gid=0, uid=6)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_sum(dt=0)
    state.check_adjustment()
    state.check_mint_sum(dt=1481901)
    state.check_adjustment()
    state.transfer(amount=518152001, from_uid=5, gid=3, to_uid=6)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1516036, gid=2)
    state.check_adjustment()
    state.withdraw(gid=0, shares=8383, uid=5)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=2077488, gid=4)
    state.check_adjustment()
    state.check_mint_sum(dt=1651952)
    state.check_adjustment()
    state.transfer(amount=654_980_875_387_579_981, from_uid=7, gid=2, to_uid=1)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.check_mint_sum(dt=1090633)
    state.check_adjustment()
    state.check_mint_sum(dt=157383)
    state.check_adjustment()
    state.transfer(amount=9868, from_uid=6, gid=3, to_uid=9)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=436087, gid=3)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=723686, uid=3)
    state.check_adjustment()
    state.time_travel(dt=215405)
    state.check_adjustment()
    state.withdraw(gid=0, shares=8_406_156_429_337_491_003_071_410, uid=8)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.withdraw(gid=4, shares=22046, uid=6)
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.claim()
    state.check_adjustment()
    state.withdraw(gid=1, shares=157, uid=8)
    state.check_adjustment()
    state.time_travel(dt=2065898)
    state.check_adjustment()
    state.transfer(amount=5_605_078_202_731_030_162_551_511, from_uid=6, gid=3, to_uid=2)
    state.check_adjustment()
    state.time_travel(dt=1141161)
    state.check_adjustment()
    state.transfer(amount=49293, from_uid=4, gid=3, to_uid=5)
    state.check_adjustment()
    state.deposit(assets=14_984_222_282_247_161_152, gid=3, uid=4)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1110321, gid=1)
    state.check_adjustment()
    state.deposit(assets=7555, gid=2, uid=1)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=715114, gid=4)
    state.check_adjustment()
    state.check_mint_sum(dt=1478487)
    state.check_adjustment()
    state.check_mint_sum(dt=1985282)
    state.check_adjustment()
    state.check_mint_sum(dt=221028)
    state.check_adjustment()
    state.transfer(amount=49361, from_uid=4, gid=0, to_uid=6)
    state.check_adjustment()
    state.withdraw(gid=1, shares=56, uid=9)
    state.check_adjustment()
    state.transfer(amount=18291, from_uid=2, gid=2, to_uid=5)
    state.check_adjustment()
    state.check_mint_sum(dt=1470046)
    state.check_adjustment()
    state.check_mint_split_between_users(dt=1959055, gid=2)
    state.teardown()


def test_gauges_fail_7(mock_lp, gauges, gc, yb, accounts, vote_for_gauges):
    for k, v in locals().items():
        setattr(StatefulG, k, v)
    state = StatefulG()
    state.check_adjustment()
    state.deposit(assets=17155, gid=0, uid=0)
    state.check_adjustment()
    state.deposit(assets=2_040_773_902, gid=2, uid=0)
    state.check_adjustment()
    state.deposit(assets=10_000_000_000_000_000_000_000_000, gid=0, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=0, uid=0)
    state.check_adjustment()
    state.deposit(assets=1, gid=0, uid=0)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=5, uid=0)
    state.check_adjustment()
    state.deposit(assets=37588, gid=3, uid=7)
    state.check_adjustment()
    state.deposit(assets=5_773_599_887_495_203_987_568_922, gid=4, uid=8)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1537601, uid=6)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=796689, uid=4)
    state.check_adjustment()
    state.deposit(assets=4631, gid=1, uid=8)
    state.check_adjustment()
    state.deposit(assets=5_773_599_887_495_203_987_568_922, gid=4, uid=8)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=1538433, uid=3)
    state.check_adjustment()
    state.check_mint_split_between_gauges(dt=5725, uid=0)
    state.teardown()
