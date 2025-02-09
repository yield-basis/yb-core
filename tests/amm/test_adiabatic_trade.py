import boa
from hypothesis import given, settings
from hypothesis import strategies as st

N_STEPS = 1000


@given(
    p_initial=st.floats(min_value=1.0, max_value=1e9),
    p_change=st.floats(min_value=0.1, max_value=10.0)
)
@settings(max_examples=10)
def test_adiabatic(collateral_token, stablecoin, amm, admin, price_oracle, p_initial, p_change):
    collateral_amount = 100 * 10**18
    collateral_token._mint_for_testing(admin, collateral_amount)
    stablecoin._mint_for_testing(amm.address, 10**60)  # Really HUGE allocation

    p = p_initial
    step_mul = p_change ** (1 / N_STEPS)

    with boa.env.prank(admin):
        price_oracle.set_price(int(p_initial * 1e18))
        debt = int(p_initial * collateral_amount / 2)
        amm._deposit(collateral_amount, debt)
        collateral_token.transfer(amm.address, collateral_amount)
        stablecoin.transferFrom(amm.address, admin, debt)

    collateral_token._mint_for_testing(admin, 10**30)
    stablecoin._mint_for_testing(admin, 10**30)

    for i in range(N_STEPS):
        p *= step_mul
        with boa.env.prank(admin):
            price_oracle.set_price(int(p * 1e18))

            collateral, debt, x0 = amm.get_state()
            # collateral * (x0 - debt) = const = I
            # p = (x0 - debt) / collateral
            # collateral = sqrt(I / p)
            # d = x0 - I / c

            Inv = (x0 - debt) * collateral / (1e18)**2
            new_collateral = (Inv / p) ** 0.5
            new_debt = x0 / 1e18 - Inv / new_collateral

            new_collateral = int(new_collateral * 1e18)
            new_debt = int(new_debt * 1e18)

            if new_collateral > collateral:
                # sell
                i = 1
                j = 0
                d_amount = new_collateral - collateral

            else:
                # buy
                i = 0
                j = 1
                d_amount = debt - new_debt

            if d_amount > 0:
                amm.exchange(i, j, d_amount, 0)

            # XXX check price and quantity
