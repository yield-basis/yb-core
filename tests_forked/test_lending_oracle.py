import boa
from tests_forked.conftest import ZERO_ADDRESS


MARKET_IDS = [3, 4, 5, 6]

ERC20_ABI = """[
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


def test_lending_oracle_vs_redemption(factory, lending_oracle, lt_deployer):
    """
    Deploy YBLendingOracle and compare its price output to preview_withdraw
    for markets 3, 4, 5, 6. The difference should be no more than 1%.
    """
    oracle = lending_oracle
    erc20 = boa.loads_abi(ERC20_ABI)

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)
        asset = erc20.at(market.asset_token)
        decimals = asset.decimals()

        oracle_price = oracle.price_in_asset(lt)
        # preview_withdraw returns asset amount for 1 LT token (10^18 shares)
        one_lt = 10**18
        redemption = lt.preview_withdraw(one_lt)

        # Normalize redemption to 10^18 scale for comparison
        redemption_normalized = redemption * 10 ** (18 - decimals)

        diff_pct = abs(int(oracle_price) - int(redemption_normalized)) * 100 / max(oracle_price, redemption_normalized)

        symbol = asset.symbol()
        print(f"\nMarket {market_id} ({symbol}):")
        print(f"  Oracle price:     {oracle_price}  ({oracle_price / 1e18:.8f} {symbol}/LT)")
        print(f"  Redemption value: {redemption_normalized}  ({redemption_normalized / 1e18:.8f} {symbol}/LT)")
        print(f"  Difference:       {diff_pct:.4f}%")

        assert diff_pct < 1, f"Market {market_id}: oracle vs redemption diff {diff_pct:.4f}% exceeds 1%"


def test_lending_oracle_balances_vs_default(factory, lending_oracle, lt_deployer):
    """
    Compare use_balances=True (balance-based fallback) to the default x0-based oracle.
    The two should be close under normal market conditions.
    """
    oracle = lending_oracle

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)

        price_default = oracle.price_in_asset(lt)
        price_balances = oracle.price_in_asset(lt, True)

        diff_pct = abs(int(price_default) - int(price_balances)) * 100 / max(price_default, price_balances)

        print(f"\nMarket {market_id}:")
        print(f"  Default (x0):    {price_default}  ({price_default / 1e18:.8f})")
        print(f"  Balances:        {price_balances}  ({price_balances / 1e18:.8f})")
        print(f"  Difference:      {diff_pct:.4f}%")

        assert diff_pct < 0.5, f"Market {market_id}: default vs balances diff {diff_pct:.4f}% exceeds 0.5%"


def test_price_in_usd_consistency(factory, lending_oracle, lt_deployer, twocrypto,
                                  cryptopool_lp_oracle_deployer):
    """
    Verify that price_in_usd == price_in_asset * asset_price_usd.
    """
    oracle = lending_oracle

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)
        pool = twocrypto.at(lt.CRYPTOPOOL())
        agg_price = cryptopool_lp_oracle_deployer.at(lt.agg()).price()

        price_asset = oracle.price_in_asset(lt)
        price_usd = oracle.price_in_usd(lt)

        # asset_price_usd = price_oracle * agg_price / 10^18
        asset_price_usd = pool.price_oracle() * agg_price // 10**18

        # price_in_usd should equal price_in_asset * asset_price_usd / 10^18
        reconstructed = price_asset * asset_price_usd // 10**18

        diff_pct = abs(int(price_usd) - int(reconstructed)) * 100 / max(price_usd, reconstructed)

        print(f"\nMarket {market_id}:")
        print(f"  price_in_usd:    {price_usd}  ({price_usd / 1e18:.2f} USD)")
        print(f"  reconstructed:   {reconstructed}  ({reconstructed / 1e18:.2f} USD)")
        print(f"  Difference:      {diff_pct:.6f}%")

        # Should be exact or near-exact (only rounding differences)
        assert diff_pct < 0.001, f"Market {market_id}: USD consistency diff {diff_pct:.6f}% exceeds 0.001%"


def test_price_in_usd_balances_vs_default(factory, lending_oracle, lt_deployer):
    """
    Compare price_in_usd with use_balances=True vs default.
    """
    oracle = lending_oracle

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)

        price_default = oracle.price_in_usd(lt)
        price_balances = oracle.price_in_usd(lt, True)

        diff_pct = abs(int(price_default) - int(price_balances)) * 100 / max(price_default, price_balances)

        print(f"\nMarket {market_id}:")
        print(f"  Default (x0):    {price_default}  ({price_default / 1e18:.2f} USD)")
        print(f"  Balances:        {price_balances}  ({price_balances / 1e18:.2f} USD)")
        print(f"  Difference:      {diff_pct:.4f}%")

        assert diff_pct < 0.5, f"Market {market_id}: USD default vs balances diff {diff_pct:.4f}% exceeds 0.5%"


def test_staked_price_matches_gauge_convert(factory, lending_oracle, lt_deployer,
                                            gauge_deployer):
    """
    staked_price_in_{asset,usd} should equal the unstaked price scaled by
    LiquidityGauge.convertToAssets(1e18) / 1e18, since _staked_scale mirrors
    the gauge's ERC4626 share->asset conversion (post-rebase).
    """
    oracle = lending_oracle

    found_any = False
    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)

        if market.staker == ZERO_ADDRESS:
            print(f"\nMarket {market_id}: no staker, skipping")
            continue
        found_any = True

        gauge = gauge_deployer.at(market.staker)
        scale = gauge.convertToAssets(10**18)

        price_asset = oracle.price_in_asset(lt)
        price_usd = oracle.price_in_usd(lt)
        staked_asset = oracle.staked_price_in_asset(lt)
        staked_usd = oracle.staked_price_in_usd(lt)

        expected_staked_asset = price_asset * scale // 10**18
        expected_staked_usd = price_usd * scale // 10**18

        # Both formulas have only 1-2 ulp of rounding difference
        asset_diff_pct = abs(int(staked_asset) - int(expected_staked_asset)) * 100 / max(staked_asset, expected_staked_asset, 1)
        usd_diff_pct = abs(int(staked_usd) - int(expected_staked_usd)) * 100 / max(staked_usd, expected_staked_usd, 1)

        print(f"\nMarket {market_id}:")
        print(f"  convertToAssets(1e18):       {scale}  ({scale / 1e18:.8f})")
        print(f"  staked_price_in_asset:       {staked_asset}")
        print(f"  expected (price * scale):    {expected_staked_asset}  diff {asset_diff_pct:.6f}%")
        print(f"  staked_price_in_usd:         {staked_usd}")
        print(f"  expected (price * scale):    {expected_staked_usd}  diff {usd_diff_pct:.6f}%")

        assert asset_diff_pct < 0.001, f"Market {market_id}: staked_asset vs gauge scale diff {asset_diff_pct:.6f}%"
        assert usd_diff_pct < 0.001, f"Market {market_id}: staked_usd vs gauge scale diff {usd_diff_pct:.6f}%"

    if not found_any:
        import pytest
        pytest.skip("no markets with a staker on this fork")


def test_staked_price_usd_asset_consistency(factory, lending_oracle, lt_deployer,
                                            twocrypto, cryptopool_lp_oracle_deployer):
    """
    staked_price_in_usd / staked_price_in_asset must equal the underlying
    asset_price_usd, same as the unstaked variants.
    """
    oracle = lending_oracle

    found_any = False
    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)

        if market.staker == ZERO_ADDRESS:
            continue
        found_any = True

        pool = twocrypto.at(lt.CRYPTOPOOL())
        agg_price = cryptopool_lp_oracle_deployer.at(lt.agg()).price()
        asset_price_usd = pool.price_oracle() * agg_price // 10**18

        staked_asset = oracle.staked_price_in_asset(lt)
        staked_usd = oracle.staked_price_in_usd(lt)
        reconstructed = staked_asset * asset_price_usd // 10**18

        diff_pct = abs(int(staked_usd) - int(reconstructed)) * 100 / max(staked_usd, reconstructed, 1)

        print(f"\nMarket {market_id}: staked_usd={staked_usd} reconstructed={reconstructed} diff={diff_pct:.6f}%")

        assert diff_pct < 0.001, f"Market {market_id}: staked USD consistency diff {diff_pct:.6f}%"

    if not found_any:
        import pytest
        pytest.skip("no markets with a staker on this fork")
