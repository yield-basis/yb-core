import boa
from hypothesis import settings, given
from hypothesis import HealthCheck
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, run_state_machine_as_test, rule  # , invariant

LEV_RATIO = 444444444444444444


class StatefulTrader(RuleBasedStateMachine):
    collateral_value = st.integers(min_value=0, max_value=10**9 * 10**18)
    debt_value = st.integers(min_value=0, max_value=10**9 * 10**18)
    is_stablecoin = st.booleans()
    frac = st.floats(min_value=0, max_value=1)
    user_id = st.integers(min_value=0, max_value=9)
    dt = st.integers(min_value=0, max_value=86400)
    rate = st.integers(min_value=0, max_value=10**18 // (365 * 86400))
    price_shift = st.floats(min_value=0.95, max_value=1.05)

    def __init__(self):
        super().__init__()
        self.value = 0
        self.state_good = True

    @rule(c_value=collateral_value, debt=debt_value)
    def deposit(self, c_value, debt):
        p = self.price_oracle.price()
        c_amount = c_value * 10**self.collateral_decimals // p
        try:
            with boa.env.prank(self.admin):
                self.amm._deposit(c_amount, debt)
                self.collateral_token.transfer(self.amm.address, c_amount)
                self.stablecoin.transferFrom(self.amm.address, self.admin, debt)
                if c_amount > 0 or debt > 0:
                    self.state_good = True
        except Exception:
            debt = self.amm.debt() + debt
            c_value = (self.amm.collateral_amount() + c_amount) * 10**(18 - self.collateral_decimals) * p // 10**18
            if c_value**2 - 4 * c_value * LEV_RATIO // 10**18 * debt < 0:
                return
            if debt >= (LEV_RATIO / 1e18 - 1 / 64) * c_value:
                return
            if debt <= c_value / 16:
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
                if frac > 0:
                    self.state_good = True
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
        value_before = self.amm.value_oracle()[1]
        with boa.env.prank(user):
            try:
                min_out = self.amm.get_dy(i, j, amount)
                with boa.reverts():
                    self.amm.exchange(i, j, amount, min_out + 1)
                self.amm.exchange(i, j, amount, min_out)
                if amount > 0:
                    self.state_good = True
            except Exception as e:
                if amount == 0:
                    return
                if is_stablecoin and amount > self.amm.get_debt():
                    return
                if 'D: uint256 = coll_value' in str(e) or 'self.get_x0' in str(e):
                    return
                if 'Bad final state' in str(e):
                    return  # Do not allow to have a loss
                if 'Unsafe min' in str(e) or 'Unsafe max' in str(e):
                    return  # Do not allow to end up in a bad state
                raise
        value_after = self.amm.value_oracle()[1]
        if self.fee > 0:
            assert value_after + value_after // 10**18 >= value_before
        else:
            assert value_after + 1 >= value_before

    @rule(dt=dt)
    def rule_propagate(self, dt):
        boa.env.time_travel(dt)

    @rule(rate=rate)
    def set_rate(self, rate):
        with boa.env.prank(self.admin):
            self.amm.set_rate(rate)

    @rule(dp=price_shift)
    def change_oracle(self, dp):
        if self.state_good:
            with boa.env.prank(self.admin):
                self.price_oracle.set_price(int(self.price_oracle.price() * dp))
                self.state_good = False

    @rule()
    def collect_fees(self):
        with boa.env.prank(self.admin):
            self.amm.collect_fees()


@given(
    collateral_decimals=st.integers(min_value=6, max_value=18),
    fee=st.integers(min_value=0, max_value=10**17),
    price=st.integers(min_value=10**17, max_value=(10 * 10**6 * 10**18))
)
@settings(max_examples=10, suppress_health_check=[HealthCheck.nested_given])
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

    StatefulTrader.TestCase.settings = settings(max_examples=2000, stateful_step_count=10, suppress_health_check=[HealthCheck.nested_given])
    for k, v in locals().items():
        setattr(StatefulTrader, k, v)
    run_state_machine_as_test(StatefulTrader)
