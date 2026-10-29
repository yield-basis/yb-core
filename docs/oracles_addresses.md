# YB oracles deployed

Deployed by `scripts/deploy_lending_oracle.py` and `scripts/deploy_lending_oracle_ll.py`; ENS:
`oracles.yieldbasis.eth` / `oracles-ll.yieldbasis.eth` -> singleton / factory,
`<label>.<parent>` -> asset oracle, `usd.<label>.<parent>` -> USD oracle
(labels: wbtc, cbbtc, tbtc, weth). Prices below were read at block 26297906.

## "Normal" variant (with `price()`)

```
==================== deployment ====================
YBPriceProxy impl : 0xb35ABc90A41eB8EacB1846e6FB8f0a29b03835bc
YBLendingOracle   : 0xB2dC48d8C09094e8426376B3C9090Eca8c11bD29
  FACTORY         : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  PROXY_IMPL      : 0xb35ABc90A41eB8EacB1846e6FB8f0a29b03835bc
---------------- per-market price() proxies ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle   : 0x1FDe332f30a78a85DeC174ac49c4bf516116AE5a   price() = 79221.94
    asset_oracle : 0x6A05dA3822d4fac6FA69298063C44258D3d6A71A   price() = 0.9852
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle   : 0xF5eDc61DFff444cf1489DfFB2A6fAEC7F989f02B   price() = 80387.71
    asset_oracle : 0x6D73ADE52CA929e41f1077754606cBE19a7f489d   price() = 1.0067
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle   : 0x77d6E2584Baa376e63B574C88A5c1b660EacFa8a   price() = 79097.64
    asset_oracle : 0x8016FC09066ec2d4A2d9306e11B6d98b7229d6A5   price() = 0.9813
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle   : 0x04C214D520c1001731BF6F1c48a91577F8cc488B   price() = 2344.15
    asset_oracle : 0xc0D7dCA93eDf05d94Da352172b837d0139cb46Bf   price() = 1.0033
====================================================
```

## LlamaLend variant where fundamental value is EMA-smoothened

```
==================== deployment ====================
YBLendingOracleLL impl   : 0xc4DBd8a2aD27A893E03973d92eFBe35E8c72f89C
YBLendingOracleLLUSD bp  : 0xE0B3810295E26536Ba460B69aF4A1e6Bc64FF934
YBLendingOracleLLFactory : 0x1B861e3b4fAc6cA259b14cC7471048bC07213315
  FACTORY                : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  LL_IMPL                : 0xc4DBd8a2aD27A893E03973d92eFBe35E8c72f89C
  USD_BLUEPRINT          : 0xE0B3810295E26536Ba460B69aF4A1e6Bc64FF934
  dao                    : 0x42F2A41A0D0e65A440813190880c8a65124895Fa
  default_ema_time       : 866 s
---------------- per-market EMA oracles ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle    : 0x05d95290E0304EcAa8442fE929C67843e4949347   price() = 79235.17
    asset_oracle  : 0xAab4E9C75d9239e21C66543039bE6766A5FbcBeB   price() = 0.9853
    pricePerShare : 1.006237
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle    : 0xD448925190ae34Cd610b857BD583C0eA696e3Bb9   price() = 80394.63
    asset_oracle  : 0xa687edb00afC851029Edf97fB6D706444563bd0f   price() = 1.0068
    pricePerShare : 1.006695
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle    : 0x1DC38DA156FB037859914a88e64b07C9B17638f5   price() = 79111.37
    asset_oracle  : 0xf525Ab8bE81A4b4A731F41BCB7d466a4d481707F   price() = 0.9815
    pricePerShare : 1.001936
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle    : 0x752718DF7FB0C29B4298Ceb82dF1ce45e6C8983e   price() = 2345.32
    asset_oracle  : 0x5ABCc01726d4479B8867FA1B55227974977a3120   price() = 1.0038
    pricePerShare : 1.003310
====================================================
```

Both variants' runtimes match `YBLendingOracle.vy` / `YBLendingOracleLL.vy` at `13cabea`: the
pool is valued at its current A during A ramps (D re-solved while the ramp is unobserved by a
trade), on top of the LL EMA following the position while untradable.

The asset prices of markets 7 and 9 read below `pricePerShare` because their pools' price_oracle
was ~13% above price_scale at that block: `pricePerShare` values the position at price_scale,
the oracles at the value after arbitrage to price_oracle.

## Superseded (do not use)

- YBLendingOracle `0x61BDFf6850ad7bBD67D3307d5c608C6e4cb3158f` (PROXY_IMPL `0x0AEE913DCDfc4a31049fa86ea78baCC931f55Aa8`)
  and YBLendingOracleLLFactory `0xd22B8683081300c9F5bE4cD001e379ed3f7d10e4` (impl `0x786c7e47341E5967d64449bB383c53bef1FcbdD2`,
  bp `0x6b5f444d8863E21F2545B0F25073b81a914B5b8d`), with their oracles: priced the pool at the A of its last
  trade with the cached D, so readings lag an idle A ramp.
- YBLendingOracle `0x58241c11abd0BDb1448EF9f38f8AA7Fda21a3A44` (PROXY_IMPL `0x7B66D1d70645d22a015a12438D42B2Aefc255D28`)
  and its proxies: no safe-debt limits, staked prices revert when untradable.
- YBLendingOracleLLFactory `0x1C4E1a6E8e470753eAA63888A6b588c8fa06e600` (impl `0x42E0D74640e5350c047134C478167f8Fc7E80C90`,
  source `6035707`: the EMA stops recording while untradable, so recovery lands on the stale pre-untradable rate),
  `0x150dD481D306A3a8577cfc8fAC195720AA3Ee7Bc` (shared EMA, no untradable cap / safe-debt limits)
  and `0x3e6B4795bd173Dd5c700cA8Cfd3f247BFcDC9D43` (separate USD/asset EMAs), with their oracles.
