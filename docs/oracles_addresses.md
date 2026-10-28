# YB oracles deployed

Deployed by `scripts/deploy_lending_oracle.py` and `scripts/deploy_lending_oracle_ll.py`; ENS:
`oracles.yieldbasis.eth` / `oracles-ll.yieldbasis.eth` -> singleton / factory,
`<label>.<parent>` -> asset oracle, `usd.<label>.<parent>` -> USD oracle
(labels: wbtc, cbbtc, tbtc, weth). Prices below were read at block 26292132.

## "Normal" variant (with `price()`)

```
==================== deployment ====================
YBPriceProxy impl : 0x0AEE913DCDfc4a31049fa86ea78baCC931f55Aa8
YBLendingOracle   : 0x61BDFf6850ad7bBD67D3307d5c608C6e4cb3158f
  FACTORY         : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  PROXY_IMPL      : 0x0AEE913DCDfc4a31049fa86ea78baCC931f55Aa8
---------------- per-market price() proxies ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle   : 0x0D9F4518aa4Eaa37338641e8145455F7ad5C4717   price() = 79322.51
    asset_oracle : 0x411fd4eeC5717263330bC5fe7ec482EB7Aab62f9   price() = 0.9846
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle   : 0x9B6c1766c98D0cfB6D748E763048E9F664A06472   price() = 81417.65
    asset_oracle : 0xaE0d2cCE2343A8916Ee6BF822Dc72C501E774682   price() = 1.0066
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle   : 0xcFC607b44FE208eE26E72F4650351311644A3E7e   price() = 79169.47
    asset_oracle : 0xf946C09fdF38d84fFB8Ab57Fd5D71C12dB95F295   price() = 0.9810
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle   : 0x14F03759a0374F215Bd33a0eC36f9d72a743060b   price() = 2328.21
    asset_oracle : 0x64DaB8F3A684f11D9e296E8e44b3b28Ff39549B2   price() = 1.0034
====================================================
```

## LlamaLend variant where fundamental value is EMA-smoothened

```
==================== deployment ====================
YBLendingOracleLL impl   : 0x786c7e47341E5967d64449bB383c53bef1FcbdD2
YBLendingOracleLLUSD bp  : 0x6b5f444d8863E21F2545B0F25073b81a914B5b8d
YBLendingOracleLLFactory : 0xd22B8683081300c9F5bE4cD001e379ed3f7d10e4
  FACTORY                : 0x370a449FeBb9411c95bf897021377fe0B7D100c0
  LL_IMPL                : 0x786c7e47341E5967d64449bB383c53bef1FcbdD2
  USD_BLUEPRINT          : 0x6b5f444d8863E21F2545B0F25073b81a914B5b8d
  dao                    : 0x42F2A41A0D0e65A440813190880c8a65124895Fa
  default_ema_time       : 866 s
---------------- per-market EMA oracles ----------------
market 7  LT 0x651D4b8168488FA163D85304662E8278d4c55BAa  (wbtc)
    usd_oracle    : 0x3557275dc8777A9f221f05f517e2D6Cd08D3D532   price() = 79365.71
    asset_oracle  : 0x8eDc4cf3e9c2caf848268c966FaEd2c6c0edac2e   price() = 0.9852
    pricePerShare : 1.006262
market 8  LT 0x722FC3640BA007C3E9867CCdB0dCa59F2e2F29F9  (cbbtc)
    usd_oracle    : 0x73d2fcfFcE9Efb4E30aAaf5CC5E0F91e250CE2c3   price() = 81407.47
    asset_oracle  : 0x481461e9b73afEFA2bd9EE6949da69ed0b8f200C   price() = 1.0065
    pricePerShare : 1.006610
market 9  LT 0x771F7290428d830ECd41E980745c327e507823Ec  (tbtc)
    usd_oracle    : 0x601EcD582609E83b4b7Db173Fd001F8E46943787   price() = 79186.84
    asset_oracle  : 0xCd6C0dBb11E2372a19cAefff2F95eaC1be3DcfB5   price() = 0.9812
    pricePerShare : 1.001953
market 10  LT 0x2B9c9f3BdcEb5d8E36a4704F08a78Fca53343cEa  (weth)
    usd_oracle    : 0x3E545603051af6A731423fe1559f597749DCfC31   price() = 2329.27
    asset_oracle  : 0xC4cD506Be8154f19DeAf8558C91F3666DaFd16a4   price() = 1.0038
    pricePerShare : 1.003378
====================================================
```

The impl's runtime matches `YBLendingOracleLL.vy` at `2f85688` (EMA keeps following the
position while untradable), which is unchanged at HEAD.

The asset prices of markets 7 and 9 read below `pricePerShare` because their pools' price_oracle
was ~13% above price_scale at that block: `pricePerShare` values the position at price_scale,
the oracles at the value after arbitrage to price_oracle.

## Superseded (do not use)

- YBLendingOracle `0x58241c11abd0BDb1448EF9f38f8AA7Fda21a3A44` (PROXY_IMPL `0x7B66D1d70645d22a015a12438D42B2Aefc255D28`)
  and its proxies: no safe-debt limits, staked prices revert when untradable.
- YBLendingOracleLLFactory `0x1C4E1a6E8e470753eAA63888A6b588c8fa06e600` (impl `0x42E0D74640e5350c047134C478167f8Fc7E80C90`,
  source `6035707`: the EMA stops recording while untradable, so recovery lands on the stale pre-untradable rate),
  `0x150dD481D306A3a8577cfc8fAC195720AA3Ee7Bc` (shared EMA, no untradable cap / safe-debt limits)
  and `0x3e6B4795bd173Dd5c700cA8Cfd3f247BFcDC9D43` (separate USD/asset EMAs), with their oracles.
