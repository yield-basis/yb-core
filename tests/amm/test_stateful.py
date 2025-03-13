import boa
from hypothesis import settings, given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule  # , invariant

LEV_RATIO = 444444444444444444


class StatefulTrader(RuleBasedStateMachine):
    collateral_value = st.integers(min_value=0, max_value=10**9 * 10**18)
    debt_value = st.integers(min_value=0, max_value=10**9 * 10**18)
    is_stablecoin = st.booleans()
    frac = st.floats(min_value=0, max_value=1)
    user_id = st.integers(min_value=0, max_value=9)

    @rule(c_value=collateral_value, debt=debt_value)
    def deposit(self, c_value, debt):
        p = self.price_oracle.price()
        c_amount = c_value * 10**self.collateral_decimals // p
        try:
            with boa.env.prank(self.admin):
                self.amm._deposit(c_amount, debt)
                self.collateral_token.transfer(self.amm.address, c_amount)
                self.stablecoin.transferFrom(self.amm.address, self.admin, debt)
        except Exception:
            debt = self.amm.debt() + debt
            c_value = (self.amm.collateral_amount() + c_amount) * 10**(18 - self.collateral_decimals) * p // 10**18
            if c_value**2 - 4 * c_value * LEV_RATIO // 10**18 * debt < 0:
                return

            raise

    @rule(frac=frac)
    def withdraw(self, frac):
        f = int(frac * 1e18)
        try:
            with boa.env.prank(self.admin):
                pair = self.amm._withdraw(f)
                self.collateral_token.transferFrom(self.amm.address, self.admin, pair[0])
                self.stablecoin.transfer(self.amm.address, pair[1])
        except Exception:
            if f == 0:
                return
            raise

    @rule(amount=debt_value, is_stablecoin=is_stablecoin, uid=user_id)
    def exchange(self, amount, is_stablecoin, uid):
        user = self.accounts[uid]
        if is_stablecoin:
            self.stablecoin._mint_for_testing(user, amount)
        else:
            amount = amount * 10**self.collateral_decimals // self.price_oracle.price()
            self.collateral_token._mint_for_testing(user, amount)
        j = int(is_stablecoin)
        i = 1 - j
        with boa.env.prank(user):
            # XXX test min_amount
            try:
                self.amm.exchange(i, j, amount, 0)
            except Exception as e:
                if amount == 0:
                    return
                if is_stablecoin and amount > self.amm.debt():
                    return
                if 'D: uint256 = coll_value' in str(e) or 'self.get_x0' in str(e):
                    return
                raise

    # invaraint to check sum of coins
    # set_price (and change the profit tracker)
    # set_rate
    # collect fees and donate
    # propagate


@given(
    collateral_decimals=st.integers(min_value=6, max_value=18),
    fee=st.integers(min_value=0, max_value=10**17),
    price=st.integers(min_value=10**17, max_value=(10 * 10**6 * 10**18))
)
@settings(max_examples=10)
def test_stateful_amm(token_mock, price_oracle, amm_deployer,
                      accounts, admin,
                      collateral_decimals, fee, price):
    stablecoin = token_mock.deploy('Stablecoin', 'xxxUSD', 18)
    collateral_token = token_mock.deploy('Collateral', 'xxxBTC', collateral_decimals)

    with boa.env.prank(admin):
        price_oracle.set_price(price)

        amm = amm_deployer.deploy(
                admin,
                stablecoin.address,
                collateral_token.address,
                2 * 10**18,
                fee,
                price_oracle.address
        )

    # Fund with stables
    with boa.env.prank(admin):
        stablecoin._mint_for_testing(amm.address, 10**12 * 10**18)  # one TRILLION dollars
        stablecoin._mint_for_testing(admin, 10**12 * 10**18)
        collateral_token._mint_for_testing(admin, 10**12 * 10**collateral_decimals)

    for a in accounts + [admin]:
        with boa.env.prank(a):
            stablecoin.approve(amm.address, 2**256-1)
            collateral_token.approve(amm.address, 2**256-1)

    StatefulTrader.TestCase.settings = settings(max_examples=200, stateful_step_count=10)
    for k, v in locals().items():
        setattr(StatefulTrader, k, v)
    run_state_machine_as_test(StatefulTrader)
