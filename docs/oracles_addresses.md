# YB oracles deployed

Deployed by `scripts/deploy_lending_oracle.py` and `scripts/deploy_lending_oracle_ll.py`; ENS:
`oracles.yieldbasis.eth` / `oracles-ll.yieldbasis.eth` -> singleton / factory,
`<label>.<parent>` -> asset oracle, `usd.<label>.<parent>` -> USD oracle
(labels: wbtc, cbbtc, tbtc, weth). Prices below were read at block 26211519.

## "Normal" variant (with `price()`)

```
==================== deployment ====================
YBPriceProxy impl : 0x0AEE913DCDfc4a31049fa86ea78baCC931f55Aa8
YBLendingOracle   : 0x61BDFf6850ad7bBD67D3307d5c608C6e4cb3158f
  FACTORY         : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  PROXY_IMPL      : 0x0AEE913DCDfc4a31049fa86ea78baCC931f55Aa8
---------------- per-market price() proxies ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle   : 0x0D9F4518aa4Eaa37338641e8145455F7ad5C4717   price() = 79881.11
    asset_oracle : 0x411fd4eeC5717263330bC5fe7ec482EB7Aab62f9   price() = 0.9781
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle   : 0x9B6c1766c98D0cfB6D748E763048E9F664A06472   price() = 81196.99
    asset_oracle : 0xaE0d2cCE2343A8916Ee6BF822Dc72C501E774682   price() = 1.0064
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle   : 0xcFC607b44FE208eE26E72F4650351311644A3E7e   price() = 79738.77
    asset_oracle : 0xf946C09fdF38d84fFB8Ab57Fd5D71C12dB95F295   price() = 0.9754
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle   : 0x14F03759a0374F215Bd33a0eC36f9d72a743060b   price() = 2423.17
    asset_oracle : 0x64DaB8F3A684f11D9e296E8e44b3b28Ff39549B2   price() = 1.0041
====================================================
```

## LlamaLend variant where fundamental value is EMA-smoothened

```
==================== deployment ====================
YBLendingOracleLL impl   : 0x42E0D74640e5350c047134C478167f8Fc7E80C90
YBLendingOracleLLUSD bp  : 0xFa57aB0dae4a00faf7F65CF418D746FC250f9Afa
YBLendingOracleLLFactory : 0x1C4E1a6E8e470753eAA63888A6b588c8fa06e600
  FACTORY                : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  LL_IMPL                : 0x42E0D74640e5350c047134C478167f8Fc7E80C90
  USD_BLUEPRINT          : 0xFa57aB0dae4a00faf7F65CF418D746FC250f9Afa
  dao                    : 0x42F2A41A0D0e65A440813190880c8a65124895Fa
  default_ema_time       : 866 s
---------------- per-market EMA oracles ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle    : 0xCF8E828d29cE630288C6Ae9D5dA5E327a401310b   price() = 79881.23
    asset_oracle  : 0x8ff647485C1A92E2B4B998d2e03b9eC66734E1ab   price() = 0.9781
    pricePerShare : 1.006983
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle    : 0x8199e25cf77729cE42d5F5892e3d72a81D4bFDEE   price() = 81197.11
    asset_oracle  : 0xC9260CCF096A2e015c28A52f89Db3274aE768531   price() = 1.0064
    pricePerShare : 1.006465
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle    : 0x125e76399211cE97B6b5413116cf0b0756f77bE9   price() = 79738.86
    asset_oracle  : 0x21B32Cf690C47195A06190A6ea4dcFB1c5059949   price() = 0.9754
    pricePerShare : 1.002804
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle    : 0x69A2E6bA8cFa2d732936334322D5E753ecBd1e86   price() = 2423.18
    asset_oracle  : 0xf82189c39e8E96DF43E942448E2a9328a8A793b6   price() = 1.0041
    pricePerShare : 1.004135
====================================================
```

The asset prices of markets 7 and 9 read below `pricePerShare` because their pools' price_oracle
was ~15% above price_scale at that block: `pricePerShare` values the position at price_scale,
the oracles at the value after arbitrage to price_oracle.

## Superseded (do not use)

- YBLendingOracle `0x58241c11abd0BDb1448EF9f38f8AA7Fda21a3A44` (PROXY_IMPL `0x7B66D1d70645d22a015a12438D42B2Aefc255D28`)
  and its proxies: no safe-debt limits, staked prices revert when untradable.
- YBLendingOracleLLFactory `0x150dD481D306A3a8577cfc8fAC195720AA3Ee7Bc` (shared EMA, no untradable cap / safe-debt limits)
  and `0x3e6B4795bd173Dd5c700cA8Cfd3f247BFcDC9D43` (separate USD/asset EMAs), with their oracles.
