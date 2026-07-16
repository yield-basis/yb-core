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

## LlamaLend variant where virtual_price is EMA-smoothened

```
==================== deployment ====================
YBLendingOracleLL impl   : 0x90E6F03E7F64dCba91A649C3AA170517d9efCA46
YBLendingOracleLLFactory : 0xFC04D5958050b8355Ad6E8DDBB6099409c44c21a
  FACTORY                : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  LL_IMPL                : 0x90E6F03E7F64dCba91A649C3AA170517d9efCA46
  dao                    : 0x42F2A41A0D0e65A440813190880c8a65124895Fa
  default_ema_time       : 866 s
---------------- per-market EMA oracles ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa
    usd_oracle    : 0xe196992a4163f702b85BBEA2F8031c773f5d893c   price() = 65219.38
    asset_oracle  : 0xf51982c7BF9C0d908692d1bB3a5627C917D6a4E2   price() = 1.0075
    pricePerShare : 1.007508
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9
    usd_oracle    : 0x42bF7C518b74417D7A4251BE6C91bC9a0FBe94B5   price() = 65096.66
    asset_oracle  : 0x5933e970A4FD346b195797b669950978cCeaa093   price() = 1.0054
    pricePerShare : 1.005456
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec
    usd_oracle    : 0xB148f19b5b522Cd3D54a199a93d307F3c79AA8Bc   price() = 64945.55
    asset_oracle  : 0xaAF4D3AE1aDC9D4b7b1c87d3129355d37F59d32c   price() = 1.0052
    pricePerShare : 1.005223
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa
    usd_oracle    : 0xD2d878499d3640a43366d384b6B98951F7c1021B   price() = 1918.56
    asset_oracle  : 0x07b173E54dF168AB2450B72FFb09aaA1E086357e   price() = 1.0134
    pricePerShare : 1.013518
====================================================
```
