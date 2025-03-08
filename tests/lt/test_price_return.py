# Stateful test where random trades are made, as well as random deposits
# however price in the AMM returns back before time increase, so price_scale is not changing.
# In such case, value oracle for an initial deposit should always go up

import boa
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule  # , invariant


class StatefulTrader(RuleBasedStateMachine):
    TEST_DEPOSIT = 10**18
    user_id = st.integers(min_value=0, max_value=9)
    amount = st.integers(min_value=0, max_value=100 * 10**18)  # Bitcoin amount to deposit
    debt_multiplier = st.floats(min_value=0, max_value=100)  # try infinity?
    withdraw_fraction = st.floats(min_value=0, max_value=2)
    is_stablecoin = st.booleans()

    def __init__(self):
        super().__init__()
        self.collateral_token._mint_for_testing(self.admin, self.TEST_DEPOSIT)
        self.p = self.cryptopool.price_oracle()
        with boa.env.prank(self.admin):
            self.test_shares = self.yb_lt.deposit(self.TEST_DEPOSIT, self.p * self.TEST_DEPOSIT // 10**18, 0)
            self.pps = self.yb_lt.pricePerShare()
        for user in self.accounts:
            self.collateral_token._mint_for_testing(user, 100 * 100 * 10**18)

    @rule(amount=amount, mul=debt_multiplier, uid=user_id)
    def deposit(self, amount, mul, uid):
        user = self.accounts[uid]
        debt = int(mul * amount * self.p / 1e18)
        with boa.env.prank(user):
            if amount == 0:
                with boa.reverts():
                    self.yb_lt.deposit(amount, debt, 0)
            else:
                try:
                    self.yb_lt.deposit(amount, debt, 0)
                except Exception:
                    if amount < 10**7:
                        # Amount being too small could be causing math precision errors in cryptoswap
                        # That will prevent a deposit, and that is normal
                        return
                    if mul > 1.5:
                        # With debt too high we revert because of discriminant being negative
                        return
                    raise

    @rule(frac=withdraw_fraction, uid=user_id)
    def withdraw(self, frac, uid):
        user = self.accounts[uid]
        shares = int(frac * self.yb_lt.balanceOf(user))
        with boa.env.prank(user):
            if frac <= 1 and shares > 0:
                try:
                    self.yb_lt.withdraw(shares, 0)
                except Exception:
                    if shares < 100_000 * 10:
                        # If share amount is too small - we may have zeroing if debt calculations
                        # That would cause a revert caused by the fact that withdrawable debt still
                        # rounds to zero cryptoassets, for example. Not an issue, but tiny amount
                        # of shares cannot be withdrawn
                        return
                    raise
            else:
                with boa.reverts():
                    self.yb_lt.withdraw(shares, 0)

    def trade_in_cryptopool(self, amount, is_stablecoin, uid):
        pass

    def trade_in_levamm(self, amount, is_stablecoin, uid):
        pass

    def propagate(self):
        pass


def test_stateful_lendborrow(cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
                             yb_allocated, seed_cryptopool, accounts, admin):
    StatefulTrader.TestCase.settings = settings(max_examples=200, stateful_step_count=10)
    for k, v in locals().items():
        setattr(StatefulTrader, k, v)
    run_state_machine_as_test(StatefulTrader)
