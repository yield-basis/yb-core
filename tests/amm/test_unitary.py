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


def test_view_methods(stablecoin, collateral_token, amm, price_oracle, admin, accounts):
    collateral_amount = 1000 * 10**18
    p_o = price_oracle.price()
    debt = (p_o * collateral_amount // 10**18) // 2
    fee = amm.fee() / 1e18

    with boa.env.prank(admin):
        amm._deposit(collateral_amount, debt)

    assert amm.get_debt() == debt
    assert amm.get_state()[:2] == (collateral_amount, debt)

    p_amm = amm.get_p()
    assert abs(p_amm - p_o) / p_o < 1e-7
    dy = amm.get_dy(0, 1, 10**10)
    p_buy = 10**10 / dy
    err_buy = 1 / dy + 1e-7
    dy = amm.get_dy(1, 0, 10**10)
    p_sell = dy / 10**10
    err_sell = 1 / dy + 1e-7
    assert abs(p_buy - p_o / (1 - fee) / 1e18) / p_buy < err_buy
    assert abs(p_sell - p_o * (1 - fee) / 1e18) / p_sell < err_sell

    assert stablecoin.address == amm.coins(0)
    assert collateral_token.address == amm.coins(1)

    _p_o, pool_value = amm.value_oracle()
    assert _p_o == p_o
    assert abs(pool_value - debt) / debt < 1e-7

    _p_o, _value = amm.value_oracle_for(collateral_amount * 2, debt * 2)
    assert _p_o == p_o
    assert abs(_value - 2 * debt) / (2 * debt) < 1e-7

    _p_o, _v_before, _v_after = amm.value_change(collateral_amount, debt, True)
    assert _p_o == p_o
    assert pool_value == _v_before
    assert abs(_v_after - 2 * _v_before) // _v_after < 1e-7

    _p_o, _v_before, _v_after = amm.value_change(collateral_amount // 2, debt // 2, False)
    assert _p_o == p_o
    assert pool_value == _v_before
    assert abs(_v_after - _v_before // 2) // _v_after < 1e-7

    assert amm.admin_fees() == 0


@given(
    collateral_amount=st.integers(min_value=0, max_value=10**25),
    debt_multiplier=st.floats(min_value=0.5, max_value=1.5),
    withdraw_fraction=st.floats(min_value=0, max_value=1.1)
)
@settings(max_examples=1000)
def test_deposit_withdraw(amm, price_oracle, admin, accounts,
                          collateral_amount, debt_multiplier, withdraw_fraction):
    p_o = price_oracle.price()
    debt = int(debt_multiplier * (p_o * collateral_amount // 10**18) / 2)

    try:
        amm.value_change(collateral_amount, debt, True)
    except Exception as e:
        if 'Unsafe min' in str(e) or 'Unsafe max' in str(e):
            return
        raise

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

    assert amm.get_debt() == debt

    state = amm.get_state()
    assert state[0] == collateral_amount
    assert state[1] == debt

    if collateral_amount > 0:
        with boa.env.prank(admin):
            frac = int(withdraw_fraction * 1e18)
            if frac <= 10**18:
                amm._withdraw(frac)
            else:
                with boa.reverts():
                    amm._withdraw(frac)


def test_exchange(collateral_token, stablecoin, amm, admin, price_oracle):
    collateral_amount = 100 * 10**18
    collateral_token._mint_for_testing(admin, collateral_amount)
    stablecoin._mint_for_testing(amm.address, 10**60)  # Really HUGE allocation
    p_o = price_oracle.price()
    debt = p_o * collateral_amount // (2 * 10**18)
    fee = amm.fee() / 1e18

    with boa.env.prank(admin):
        amm._deposit(collateral_amount, debt)
        collateral_token.transfer(amm.address, collateral_amount)
        stablecoin.transferFrom(amm.address, admin, debt)

        c0 = collateral_token.balanceOf(admin)
        s0 = stablecoin.balanceOf(admin)

        expected_out = amm.get_dy(0, 1, 10**18)
        with boa.reverts():
            amm.exchange(0, 1, 10**18, int(1.0001 * expected_out))

        out0 = amm.exchange(0, 1, 10**18, expected_out)

        c1 = collateral_token.balanceOf(admin)
        s1 = stablecoin.balanceOf(admin)

        assert s0 - s1 == 10**18
        assert c1 - c0 == out0
        assert abs(10**18 * (1 - fee) / out0 - p_o / 1e18) / (p_o / 1e18) < 1e-6

        collateral_token._mint_for_testing(admin, 10**13)
        c1 += 10**13

        expected_out = amm.get_dy(1, 0, 10**13)
        with boa.reverts():
            amm.exchange(1, 0, 10**13, int(1.0001 * expected_out))

        out1 = amm.exchange(1, 0, 10**13, expected_out)

        c2 = collateral_token.balanceOf(admin)
        s2 = stablecoin.balanceOf(admin)

        assert s2 - s1 == out1
        assert c1 - c2 == 10**13
        assert abs(out1 / (1 - fee) / 10**13 - p_o / 1e18) / (p_o / 1e18) < 1e-6
