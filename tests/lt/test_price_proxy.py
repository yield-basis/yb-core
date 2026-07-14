"""
YBPriceProxy: per-market thin price() proxies spawned by YBLendingOracle.

create_oracles(market_id) resolves the LT from the factory market id and clones the impl
twice (USD + asset denomination). Each clone's price() must equal the singleton's
price_in_usd / price_in_asset for that LT; creation is public and idempotent.
"""
import boa

ZERO = "0x0000000000000000000000000000000000000000"


def _open_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin):
    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)
    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, p, 0)


def _market_id(factory, yb_lt):
    n = factory.market_count()
    return next(i for i in range(n) if factory.markets(i).lt == yb_lt.address)


def test_create_and_price(
    lending_oracle, factory, yb_lt, price_proxy_deployer,
    cryptopool, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool,
):
    _open_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    market_id = _market_id(factory, yb_lt)

    # Anyone can spawn the proxies.
    with boa.env.prank(accounts[4]):
        usd, asset = lending_oracle.create_oracles(market_id)

    assert usd != ZERO and asset != ZERO and usd != asset
    assert lending_oracle.usd_oracle(market_id) == usd
    assert lending_oracle.asset_oracle(market_id) == asset

    usd_p = price_proxy_deployer.at(usd)
    asset_p = price_proxy_deployer.at(asset)

    # Each clone is bound to the singleton, this LT and its denomination.
    assert usd_p.oracle() == lending_oracle.address
    assert usd_p.lt() == yb_lt.address
    assert usd_p.in_usd() is True
    assert asset_p.in_usd() is False

    # price() forwards to the matching singleton view, bit-for-bit.
    assert usd_p.price() == lending_oracle.price_in_usd(yb_lt.address)
    assert asset_p.price() == lending_oracle.price_in_asset(yb_lt.address)
    assert usd_p.price() > 0 and asset_p.price() > 0

    # Idempotent: a second call returns the same pair without redeploying.
    with boa.env.prank(accounts[5]):
        usd2, asset2 = lending_oracle.create_oracles(market_id)
    assert (usd2, asset2) == (usd, asset)


def test_nonexistent_market_reverts(lending_oracle, factory):
    with boa.reverts("No market"):
        lending_oracle.create_oracles(factory.market_count() + 100)


def test_reinitialize_reverts(lending_oracle, factory, yb_lt, price_proxy_deployer):
    market_id = _market_id(factory, yb_lt)
    usd, _asset = lending_oracle.create_oracles(market_id)
    with boa.reverts("Initialized"):
        price_proxy_deployer.at(usd).initialize(lending_oracle.address, yb_lt.address, True)
