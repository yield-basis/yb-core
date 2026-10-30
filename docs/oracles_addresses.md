# YB oracles deployed

Deployed by `scripts/deploy_lending_oracle.py` and `scripts/deploy_lending_oracle_ll.py`; ENS:
`oracles.yieldbasis.eth` / `oracles-ll.yieldbasis.eth` -> singleton / factory,
`<label>.<parent>` -> asset oracle, `usd.<label>.<parent>` -> USD oracle
(labels: wbtc, cbbtc, tbtc, weth). Prices below were read at block 26306644.

## "Normal" variant (with `price()`)

```
==================== deployment ====================
YBPriceProxy impl : 0x19e6ddC0A6C7f3cB98005dbbFA18583cc3584512
YBLendingOracle   : 0xcE43c6668945FEfa079Da1c38F7CDab5A525Ec7C
  FACTORY         : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  PROXY_IMPL      : 0x19e6ddC0A6C7f3cB98005dbbFA18583cc3584512
---------------- per-market price() proxies ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle   : 0x4542410d3Ab5Cb87d52CeEad9C991eD9Ad1f6F92   price() = 77278.05
    asset_oracle : 0x241c7cc384A4cDF13ce594eC517767dE19bE6437   price() = 0.9981
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle   : 0xfaD8a3aE313A6311C0d05D154e60207b5e0Bbb50   price() = 78127.81
    asset_oracle : 0x24191d6e3E6F5EAed4A7D5aA859957de5E9020B4   price() = 1.0078
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle   : 0x84b5D551Cf9C1612517B6489b7257C42C235ed22   price() = 76968.43
    asset_oracle : 0x3BB86AB5c58773b0CCD9c989A2AaD5Cb61897705   price() = 0.9930
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle   : 0x4A48a3217D04997ea4b78687f2B50DDa5774F03F   price() = 2339.93
    asset_oracle : 0x4e1d5D0221a6CFdF548501319DFa81D0515404A8   price() = 1.0446
====================================================
```

## LlamaLend variant where fundamental value is EMA-smoothened

```
==================== deployment ====================
YBLendingOracleLL impl   : 0x4CaD07Fb9875bEBF18d66F1796e57C2AbEF1eE23
YBLendingOracleLLUSD bp  : 0xC1665877021c0824Fee450001C53027E3B168628
YBLendingOracleLLFactory : 0xaC866A97FB35fdE170853d3dA58Da7195F97f7f8
  FACTORY                : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  LL_IMPL                : 0x4CaD07Fb9875bEBF18d66F1796e57C2AbEF1eE23
  USD_BLUEPRINT          : 0xC1665877021c0824Fee450001C53027E3B168628
  dao                    : 0x42F2A41A0D0e65A440813190880c8a65124895Fa
  default_ema_time       : 866 s
---------------- per-market EMA oracles ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle    : 0xe3F882972349AC1369AB7393B219016ff7254B42   price() = 77282.18
    asset_oracle  : 0x8F9F1C2f705f1EBBB9feedBb5f5672F4F4c5f130   price() = 0.9981
    pricePerShare : 1.005857
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle    : 0x36021CbF8a01f0219e9d0b6Db5BA0fE2FB11c52C   price() = 78132.40
    asset_oracle  : 0xC000df0824E75Ad1172b2ee040117a54C7795Be7   price() = 1.0079
    pricePerShare : 1.007855
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle    : 0x6Cf41C0AA9287f40d0313d5336095e69377a0Cb4   price() = 76972.51
    asset_oracle  : 0x24AD01Ebff47b6539B53A006d0222a9D165B8c95   price() = 0.9930
    pricePerShare : 1.001463
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle    : 0x9AFaa4A3ab8ff212214C4c63231a1ce960B6bc03   price() = 2339.88
    asset_oracle  : 0x340C865d12699be858Be3bde8EbB283582eB768b   price() = 1.0445
    pricePerShare : 1.044664
====================================================
```

Both variants' runtimes match `YBLendingOracle.vy` / `YBLendingOracleLL.vy` at `f23158f`: a
killed AMM is priced at exactly what `LT.emergency_withdraw` pays (the balance value, without
holding back the admin fee buffer), on top of valuing the pool at its current A during A ramps
and the LL EMA following the position while untradable.

The asset prices of markets 7 and 9 read below `pricePerShare` because their pools' price_oracle
was ~7.5-8% above price_scale at that block: `pricePerShare` values the position at price_scale,
the oracles at the value after arbitrage to price_oracle.

## Superseded (do not use)

- YBLendingOracle `0xB2dC48d8C09094e8426376B3C9090Eca8c11bD29` (PROXY_IMPL `0xb35ABc90A41eB8EacB1846e6FB8f0a29b03835bc`)
  and YBLendingOracleLLFactory `0x1B861e3b4fAc6cA259b14cC7471048bC07213315` (impl `0xc4DBd8a2aD27A893E03973d92eFBe35E8c72f89C`,
  bp `0xE0B3810295E26536Ba460B69aF4A1e6Bc64FF934`), with their oracles: priced a killed AMM as if it still traded
  (the arbitrage value, less the admin fee buffer), below what `emergency_withdraw` pays.
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
