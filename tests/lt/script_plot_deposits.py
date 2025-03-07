import boa
import pylab
import numpy as np


# It is not a test but a plotting script
def test_deposit_lt(cryptopool, seed_cryptopool, yb_allocated, yb_lt, yb_amm, collateral_token, stablecoin, admin):
    # 100k USD + 100k worth BTC in pool
    collateral_token._mint_for_testing(admin, 10 * 10**18)
    stablecoin._mint_for_testing(admin, 10**6 * 10**18)
    deposit_amount = 10**18
    p = 100_000
    relative_changes = np.linspace(0, 0.5, 100)
    relative_changes = list(-relative_changes[1:][::-1]) + list(relative_changes)
    misbalances = []
    values = []

    with boa.env.prank(admin):
        yb_lt.deposit(deposit_amount, p * deposit_amount, 0)
        collateral0, debt0, _ = yb_amm.get_state()

        for r in list(relative_changes):
            with boa.env.anchor():
                # Prepare state - disbalance
                if r > 0:
                    amount = int(r * debt0)
                    yb_amm.exchange(0, 1, amount, 0)
                elif r < 0:
                    amount = int(-r * collateral0)
                    yb_amm.exchange(1, 0, amount, 0)
                misbalances.append(r)
                value = yb_lt.preview_deposit(deposit_amount, p * deposit_amount)
                values.append(value)

    pylab.plot(misbalances, values)
    pylab.show()


# It is not a test but a plotting script
def test_deposit_cryptopool(cryptopool, seed_cryptopool, yb_allocated, yb_lt, yb_amm, collateral_token, stablecoin, admin):
    # 100k USD + 100k worth BTC in pool
    collateral_token._mint_for_testing(admin, 10 * 10**18)
    stablecoin._mint_for_testing(admin, 10**6 * 10**18)
    deposit_amount = 10**18
    p = 100_000
    relative_changes = np.linspace(0, 0.2, 100)
    relative_changes = list(-relative_changes[1:][::-1]) + list(relative_changes)
    misbalances = []
    values_by_price = []
    values_by_balances = []

    with boa.env.prank(admin):
        yb_lt.deposit(deposit_amount, p * deposit_amount, 0)
        stablecoins = cryptopool.balances(0)
        collateral = cryptopool.balances(1)

        for r in list(relative_changes):
            with boa.env.anchor():
                # Prepare state - disbalance
                if r > 0:
                    amount = int(r * stablecoins)
                    cryptopool.exchange(0, 1, amount, 0)
                elif r < 0:
                    amount = int(-r * collateral)
                    cryptopool.exchange(1, 0, amount, 0)
                misbalances.append(r)
                value = yb_lt.preview_deposit(deposit_amount, p * deposit_amount)
                values_by_price.append(value)
                value = yb_lt.preview_deposit(deposit_amount, deposit_amount * cryptopool.balances(0) // cryptopool.balances(1))
                values_by_balances.append(value)

    pylab.plot(misbalances, values_by_price, c="blue")
    pylab.plot(misbalances, values_by_balances, c="red")
    pylab.show()
