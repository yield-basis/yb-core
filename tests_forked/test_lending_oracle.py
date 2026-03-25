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

        oracle_price = oracle.price(lt)
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
