# Stateful test where random trades are made, as well as random deposits
# however price in the AMM returns back before time increase, so price_scale is not changing.
# In such case, value oracle for an initial deposit should always go up

import boa
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule, invariant


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
        self.pps = self.yb_lt.pricePerShare()
        for user in self.accounts:
            self.collateral_token._mint_for_testing(user, 100 * 100 * 10**18)

        # Reserves for trading
        self.collateral_token._mint_for_testing(self.admin, 10**36)
        self.stablecoin._mint_for_testing(self.admin, 100_000 * 10**36)

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

                    balances = [self.cryptopool.balances(0) + debt, (self.cryptopool.balances(1) + amount) * self.p / 1e18]
                    if abs(balances[0] - balances[1]) / (2 * min(balances)) > 10:
                        # May revert due to pool being too imbalanced
                        return

                    if mul > 1.5:
                        # With debt too high we revert because of discriminant being negative
                        return

                    p_o_pool = self.cryptopool_oracle.price()
                    amm_collateral_value = self.yb_amm.collateral_amount() * p_o_pool // 10**18
                    amm_debt_value = self.yb_amm.debt()
                    lp_tokens = self.cryptopool.calc_token_amount([debt, amount], True)
                    amm_debt_value += debt
                    amm_collateral_value += lp_tokens * p_o_pool // 10**18

                    if amm_collateral_value**2 < 4 * amm_collateral_value * amm_debt_value * 4 / 9 * 0.999:
                        # Discriminant is too close to negative
                        return

                    raise

    @rule(frac=withdraw_fraction, uid=user_id)
    def withdraw(self, frac, uid):
        user = self.accounts[uid]
        user_shares = self.yb_lt.balanceOf(user)
        shares = int(frac * user_shares)
        with boa.env.prank(user):
            if shares <= user_shares and shares > 0:
                try:
                    self.yb_lt.withdraw(shares, 0)
                except Exception:
                    # Failures could be if pool is too imbalanced to return the amount of debt requested
                    # or number of shares being too close to 0 thus returning zero debt
                    return
            else:
                with boa.reverts():
                    self.yb_lt.withdraw(shares, 0)

    @rule(amount=amount, is_stablecoin=is_stablecoin)
    def trade_in_cryptopool(self, amount, is_stablecoin):
        if is_stablecoin:
            i = 0
            j = 1
            amount = amount * self.p // 10**18
        else:
            i = 1
            j = 0
        with boa.env.prank(self.admin):
            try:
                self.cryptopool.exchange(i, j, amount, 0)
            except Exception:
                # We are not testing exchanges here, so we are not checking all the corner cases where it may revert
                return

    @rule(amount=amount, is_stablecoin=is_stablecoin)
    def trade_in_levamm(self, amount, is_stablecoin):
        if is_stablecoin:
            self.stablecoin._mint_for_testing(self.admin, amount)
            with boa.env.prank(self.admin):
                try:
                    self.yb_amm.exchange(0, 1, amount, 0)
                except Exception:
                    if self.yb_amm.debt() > amount:
                        return
                    raise
        else:
            pass


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

    @invariant()
    def uponly(self):
        pps = self.yb_lt.pricePerShare()
        assert pps + pps // 10**16 >= self.pps
        self.pps = pps


def test_stateful_lendborrow(cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, cryptopool_oracle,
                             yb_allocated, seed_cryptopool, accounts, admin):
    StatefulTrader.TestCase.settings = settings(max_examples=2000, stateful_step_count=10)
    for k, v in locals().items():
        setattr(StatefulTrader, k, v)
    run_state_machine_as_test(StatefulTrader)
