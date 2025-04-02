# Stateful test where random trades are made, as well as random deposits
# however price in the AMM returns back before time increase, so price_scale is not changing.
# In such case, value oracle for an initial deposit should always go up

import boa
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, invariant

from collections import namedtuple


ValuesOut = namedtuple('ValuesOut', ['admin', 'total', 'ideal_staked', 'staked', 'staked_tokens', 'supply_tokens'])


class StatefulTrader(RuleBasedStateMachine):
    TEST_DEPOSIT = 10**18
    user_id = st.integers(min_value=0, max_value=9)
    amount = st.integers(min_value=0, max_value=100 * 10**18)  # Bitcoin amount to deposit
    debt_multiplier = st.floats(min_value=0, max_value=100)  # try infinity?
    withdraw_fraction = st.floats(min_value=0, max_value=2)
    is_stablecoin = st.booleans()
    dt = st.integers(min_value=0, max_value=86400)

    def __init__(self):
        super().__init__()
        self.collateral_token._mint_for_testing(self.admin, self.TEST_DEPOSIT)
        self.p = self.cryptopool.price_oracle()
        with boa.env.prank(self.admin):
            self.test_shares = self.yb_lt.deposit(self.TEST_DEPOSIT, self.p * self.TEST_DEPOSIT // 10**18, 0)
            self.yb_lt.set_rate(0)
        for user in self.accounts:
            self.collateral_token._mint_for_testing(user, 100 * 100 * 10**18)

    def get_lv(self, p_o=None):
        if p_o is None:
            p_o = self.cryptopool.price_oracle()
        return ValuesOut(*self.yb_lt.internal._calculate_values(p_o))

    @rule(amount=amount, mul=debt_multiplier, uid=user_id)
    def deposit(self, amount, mul, uid):
        user = self.accounts[uid]
        debt = int(mul * amount * self.p / 1e18)
        staked_before = self.get_lv().staked
        with boa.env.prank(user):
            try:
                self.yb_lt.deposit(amount, debt, 0)
            except Exception:
                # We are not testing this function, this is tested elsewhere. So no tests
                return
        staked_after = self.get_lv().staked
        assert staked_after == staked_before

    @rule(frac=withdraw_fraction, uid=user_id)
    def withdraw(self, frac, uid):
        user = self.accounts[uid]
        user_shares = self.yb_lt.balanceOf(user)
        shares = int(frac * user_shares)
        staked_before = self.get_lv().staked
        with boa.env.prank(user):
            try:
                self.yb_lt.withdraw(shares, 0)
            except Exception:
                # We are not testing this function, this is tested elsewhere. So no tests
                return
        staked_after = self.get_lv().staked
        assert staked_after == staked_before

    @rule(frac=withdraw_fraction, uid=user_id)
    def stake(self, frac, uid):
        user = self.accounts[uid]
        user_lt = self.yb_lt.balanceOf(user)
        lt = int(frac * user_lt)
        with boa.env.prank(user):
            try:
                self.yb_staker.deposit(lt, user)
            except Exception:
                if lt > user_lt:
                    return
                raise

    @rule(frac=withdraw_fraction, uid=user_id)
    def unstake(self, frac, uid):
        user = self.accounts[uid]
        user_shares = self.yb_staker.balanceOf(user)
        shares = int(frac * user_shares)
        with boa.env.prank(user):
            try:
                self.yb_staker.redeem(shares, user, user)
            except Exception:
                if shares > user_shares:
                    return
                raise

    @rule(dt=dt)
    def propagate(self, dt):
        # Deposit and withdraw to make AMM balanced
        b0 = self.cryptopool.balances(0)
        b1 = self.cryptopool.balances(1)
        diff = b0 - b1 * self.p // 10**18
        with boa.env.prank(self.admin):
            if diff < 0:
                to_deposit = -diff
                if to_deposit > 0:
                    self.stablecoin._mint_for_testing(self.admin, to_deposit)
                    try:
                        self.cryptopool.add_liquidity([to_deposit, 0], 0)
                    except Exception:
                        pass  # Small deposits might fail due to arithmetic errors
            else:
                to_deposit = diff * 10**18 // self.p
                if to_deposit > 0:
                    self.collateral_token._mint_for_testing(self.admin, to_deposit)
                    try:
                        self.cryptopool.add_liquidity([0, to_deposit], 0)
                    except Exception:
                        pass  # Small deposits might fail due to arithmetic errors

        boa.env.time_travel(dt)

    @rule()
    def record_staked_values(self):
        self.lv = self.get_lv()

    @rule()
    def withdraw_admin_fees(self):
        with boa.env.prank(self.admin):
            self.yb_lt.withdraw_admin_fees()

    @invariant()
    def staked_fractions(self):
        p_o = self.cryptopool.price_oracle()
        lv = self.get_lv(p_o)

        assert lv.admin + lv.total == self.yb_amm.value_oracle()[1] * 10**18 // p_o
        assert abs(lv.staked / lv.total - lv.staked_tokens / lv.supply_tokens) < 1e-10

        if hasattr(self, 'lv'):
            if lv.staked_tokens > 0 and self.lv.staked_tokens > 0:
                # Value per LP token can either stay the same or decrease
                assert lv.staked / (lv.staked_tokens + 1) <= self.lv.staked / self.lv.staked_tokens

            if lv.supply_tokens > lv.staked_tokens and self.lv.supply_tokens > self.lv.staked_tokens:
                assert (lv.total - lv.staked) / (lv.supply_tokens - lv.staked_tokens) >= (self.lv.total - self.lv.staked) / (self.lv.supply_tokens - self.lv.staked_tokens + 1)

        self.lv = lv


def test_price_return(cryptopool, yb_lt, yb_amm, yb_staker, collateral_token, stablecoin, cryptopool_oracle,
                      yb_allocated, seed_cryptopool, accounts, admin):
    StatefulTrader.TestCase.settings = settings(max_examples=500, stateful_step_count=10)
    for k, v in locals().items():
        setattr(StatefulTrader, k, v)

    assert yb_lt.staker() == yb_staker.address

    run_state_machine_as_test(StatefulTrader)
