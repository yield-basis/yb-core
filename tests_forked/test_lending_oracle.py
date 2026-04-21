import boa


MARKET_IDS = [3, 4, 5, 6]

ERC20_ABI = """[
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"symbol","outputs":[{"type":"string"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


def test_lending_oracle_vs_redemption(factory, forked_env):
    """
    Deploy YBLendingOracle and compare its price output to preview_withdraw
    for markets 3, 4, 5, 6. The difference should be no more than 1%.
    """
    oracle = boa.load("contracts/utils/YBLendingOracle.vy")
    lt_deployer = boa.load_partial("contracts/LT.vy")
    erc20 = boa.loads_abi(ERC20_ABI)

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt_addr = market[3]  # lt field
        asset_addr = market[0]  # asset_token field

        lt = lt_deployer.at(lt_addr)
        asset = erc20.at(asset_addr)
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


def test_lending_oracle_balances_vs_default(factory, forked_env):
    """
    Compare use_balances=True (balance-based fallback) to the default x0-based oracle.
    The two should be close under normal market conditions.
    """
    oracle = boa.load("contracts/utils/YBLendingOracle.vy")
    lt_deployer = boa.load_partial("contracts/LT.vy")

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt_addr = market[3]
        lt = lt_deployer.at(lt_addr)

        price_default = oracle.price_in_asset(lt)
        price_balances = oracle.price_in_asset(lt, True)

        diff_pct = abs(int(price_default) - int(price_balances)) * 100 / max(price_default, price_balances)

        print(f"\nMarket {market_id}:")
        print(f"  Default (x0):    {price_default}  ({price_default / 1e18:.8f})")
        print(f"  Balances:        {price_balances}  ({price_balances / 1e18:.8f})")
        print(f"  Difference:      {diff_pct:.4f}%")

        assert diff_pct < 0.5, f"Market {market_id}: default vs balances diff {diff_pct:.4f}% exceeds 0.5%"


def test_price_in_usd_consistency(factory, forked_env):
    """
    Verify that price_in_usd == price_in_asset * asset_price_usd.
    """
    oracle = boa.load("contracts/utils/YBLendingOracle.vy")
    lt_deployer = boa.load_partial("contracts/LT.vy")
    twocrypto = boa.load_partial("contracts/twocrypto_ng/contracts/main/Twocrypto.vy")

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market[3])
        pool = twocrypto.at(lt.CRYPTOPOOL())
        agg_price = boa.load_partial("contracts/CryptopoolLPOracle.vy").at(lt.agg()).price()

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


def test_price_in_usd_balances_vs_default(factory, forked_env):
    """
    Compare price_in_usd with use_balances=True vs default.
    """
    oracle = boa.load("contracts/utils/YBLendingOracle.vy")
    lt_deployer = boa.load_partial("contracts/LT.vy")

    for market_id in MARKET_IDS:
        market = factory.markets(market_id)
        lt = lt_deployer.at(market[3])

        price_default = oracle.price_in_usd(lt)
        price_balances = oracle.price_in_usd(lt, True)

        diff_pct = abs(int(price_default) - int(price_balances)) * 100 / max(price_default, price_balances)

        print(f"\nMarket {market_id}:")
        print(f"  Default (x0):    {price_default}  ({price_default / 1e18:.2f} USD)")
        print(f"  Balances:        {price_balances}  ({price_balances / 1e18:.2f} USD)")
        print(f"  Difference:      {diff_pct:.4f}%")

        assert diff_pct < 0.5, f"Market {market_id}: USD default vs balances diff {diff_pct:.4f}% exceeds 0.5%"
