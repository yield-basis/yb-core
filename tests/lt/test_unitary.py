PRICE = 100_000


def test_something(cryptopool, cryptopool_oracle, yb_lt, stablecoin, collateral_token, accounts):
    user = accounts[0]
    collateral_token._mint_for_testing(user, 10**18)
    stablecoin._mint_for_testing(user, PRICE * 10**18)
