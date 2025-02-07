import boa
from hypothesis import given, settings
from hypothesis import strategies as st


@given(
    rate=st.integers(min_value=0, max_value=10**18),
    dt=st.integers(min_value=0, max_value=365*86400)
)
@settings(max_examples=100)
def test_set_rate(amm, rate, dt, admin, accounts):
    rate_mul_0 = amm.get_rate_mul()
    with boa.reverts():
        with boa.env.prank(accounts[0]):
            amm.set_rate(rate)
    with boa.env.prank(admin):
        amm.set_rate(rate)
    boa.env.time_travel(dt)
    rate_mul_1 = amm.get_rate_mul()
    assert rate_mul_1 == rate_mul_0 * (10**18 + dt * rate) // 10**18


@given(
    collateral_amount=st.integers(min_value=0, max_value=10**25),
    debt_multiplier=st.floats(min_value=0.9, max_value=1.1),
    withdraw_fraction=st.floats(min_value=0, max_value=1.1)
)
@settings(max_examples=1000)
def test_deposit_withdraw(stablecoin, collateral_token, amm, price_oracle,
                          admin, accounts,
                          collateral_amount, debt_multiplier, withdraw_fraction):
    p_o = price_oracle.price()
    debt = int(debt_multiplier * (p_o * collateral_amount // 10**18) / 2)

    with boa.env.prank(accounts[0]):
        with boa.reverts('Access violation'):
            amm._deposit(collateral_amount, debt)

    with boa.env.prank(admin):
        p_o_amm, value_before, value_after = amm._deposit(collateral_amount, debt)
        assert p_o_amm == p_o
        assert value_before == 0
        if collateral_amount > 0:
            assert value_after > 0
        else:
            assert value_after == 0

    with boa.env.prank(accounts[0]):
        with boa.reverts('Access violation'):
            amm._withdraw(10**18)

    if collateral_amount > 0:
        with boa.env.prank(admin):
            frac = int(withdraw_fraction * 1e18)
            if frac <= 10**18:
                amm._withdraw(frac)
            else:
                with boa.reverts():
                    amm._withdraw(frac)
