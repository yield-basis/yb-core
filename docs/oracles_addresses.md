# YB oracles deployed

## "Normal" variant (with `price()`)

```
==================== deployment ====================
YBPriceProxy impl : 0x7B66D1d70645d22a015a12438D42B2Aefc255D28
YBLendingOracle   : 0x58241c11abd0BDb1448EF9f38f8AA7Fda21a3A44
  FACTORY         : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  PROXY_IMPL      : 0x7B66D1d70645d22a015a12438D42B2Aefc255D28
---------------- per-market price() proxies ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa
    usd_oracle   : 0x0e0bAa1B3C3cA2f9555A97B68098b56E36f7A41A   price() = 64458.29
    asset_oracle : 0x881511e11b3fc4179A841B0202b4D0c3f3F11B2E   price() = 1.0080
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9
    usd_oracle   : 0x1BAAE8dc59C2C80dd95B157e83980A3022EC8036   price() = 65384.90
    asset_oracle : 0x7a1F7eABEC39fdfeC8b143714571edF007199ba8   price() = 1.0050
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec
    usd_oracle   : 0x10Fd9023601D4F680805C1f041a2e08Ec636B7Ae   price() = 64223.67
    asset_oracle : 0x4b327a2211A06e98B840f626311451a7D191247F   price() = 1.0055
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa
    usd_oracle   : 0x12330c6D629EB6314c6f5cfc370CE608F3e72b7B   price() = 1914.42
    asset_oracle : 0x056f706d81Ee801DB2611c719861eBfe70c80147   price() = 1.0130
====================================================
```

## LlamaLend variant where fundamental value is EMA-smoothened

```
==================== deployment ====================
YBLendingOracleLL impl   : 0xBD04c291Ed65c8cf7395C7B34b4f4169598E199C
YBLendingOracleLLFactory : 0x3e6B4795bd173Dd5c700cA8Cfd3f247BFcDC9D43
  FACTORY                : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  LL_IMPL                : 0xBD04c291Ed65c8cf7395C7B34b4f4169598E199C
  dao                    : 0x42F2A41A0D0e65A440813190880c8a65124895Fa
  default_ema_time       : 866 s
---------------- per-market EMA oracles ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle    : 0x065fc25c5EC16E3330b5B3985934C58022ff7e11   price() = 64429.21
    asset_oracle  : 0xffffa27e18CD37E438572A2189F7e4c09DaD73FC   price() = 1.0074
    pricePerShare : 1.007412
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle    : 0x6fA25B793D37040334d73956e0b30a665b9DC5Cc   price() = 64766.25
    asset_oracle  : 0xabCdb24A676c3214497ACe40f9ee8f898Da10602   price() = 1.0053
    pricePerShare : 1.005356
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle    : 0x5d550ba2d9839097E8E1b615C6f3c2d8ebF898F6   price() = 65718.46
    asset_oracle  : 0xFdf3CD72cCb3b9C9C233B738A54c5cc9fF97bf0D   price() = 1.0048
    pricePerShare : 1.004791
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle    : 0xd2a7eEAfC8896FF940A9f9980382b4088A071472   price() = 1872.01
    asset_oracle  : 0xc3D6466c9ad13be3bA90885EBCAD0bC76EefBA8A   price() = 1.0125
    pricePerShare : 1.012557
====================================================
```
