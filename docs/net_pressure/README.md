# Net-Pressure Incentives

Adaptive incentive system that compensates for crvUSD **net pressure** in YB markets:
it diverts a DAO-set slice of protocol fees into a reserve and uses a PID controller
to pay a savings-rate bonus that attracts crvUSD into a "sink" pool, relieving the
pressure.

The research and calibration behind it live in
[`REPORT_dynamic_incentives.md`](./REPORT_dynamic_incentives.md) (with figures in
[`pics/`](./pics)). This file summarizes the on-chain design in
`contracts/net_pressure/`.

## What is "net pressure"?

For a YB market the AMM holds Curve LP tokens (crvUSD/crypto) financed by `debt`
(crvUSD). **Net pressure = debt − crvUSD sitting inside those LP tokens**:

- **positive** → unwinding the position must *buy* crvUSD to repay (crvUSD buy pressure);
- **negative** → the LP already holds more crvUSD than the debt (sell pressure).

A settled 2× leveraged 50/50 LP has `debt == crvUSD-in-LP`, so net pressure is ~0 in
equilibrium and only deviates when the pool/AMM are pushed off it — that deviation is
the signal the controller acts on.

## Architecture

```
LTs ──admin-fee LT shares──▶ FeeSplitter ──(1−frac)──▶ FeeDistributor (veYB, unchanged)
                                  │ (frac)
                                  ▼
                                 PID  ──converts LT→crvUSD reserve
                                      ──sets crvUSD/sec rate──▶ FastGauge ──streams crvUSD──▶ stakers
                                                                   ▲
                                              market rate ── MarketRateGetter (sUSDS)
                                              net pressure ── YBNetPressure (oracle)
```

| Contract | Role |
|---|---|
| [`FeeSplitter.vy`](../../contracts/net_pressure/FeeSplitter.vy) | Installed as `Factory.fee_receiver`. Reads the LT token set from the FeeDistributor, sends `split_fraction` of each LT balance to the PID and the rest to the FeeDistributor, then pokes `PID.trigger()` and `FeeDistributor.fill_epochs()`. |
| [`PID.vy`](../../contracts/net_pressure/PID.vy) | Converts the LT fees it receives into a crvUSD reserve, runs the control loop on aggregate net pressure, and sets the FastGauge stream rate. Holds the reserve. |
| [`FastGauge.vy`](../../contracts/net_pressure/FastGauge.vy) | ERC4626 staking gauge over a Curve **stableswap** LP (the sink). Streams a single reward (crvUSD) at a rate only the PID sets; pulls crvUSD from the PID at checkpoint. |
| [`MarketRateGetter.vy`](../../contracts/net_pressure/MarketRateGetter.vy) | Reports the "market rate" the offer is quoted against. First implementation reads the Sky Savings Rate (sUSDS). Swappable by the DAO. |
| [`YBNetPressure.vy`](../../contracts/net_pressure/YBNetPressure.vy) | Manipulation-resistant net-pressure oracle (`net_pressure_oracle`) and the AMM's half-TVL (`half_tvl_oracle` = its equity at `price_oracle`, the normalizer); `net_pressure_and_tvl` returns both in one call (sharing the `lp_oracle_2` solve) for the controller's per-pool loop. |

## Manipulation resistance

Every quantity the controller consumes is measured at the pool's `price_oracle`
(EMA), never from spot balances:

- **Net pressure & half-TVL** (the normalizer) come from `YBNetPressure`, both derived
  from the AMM's **conserved invariants** (`x0` and the constant product `k`) marked at
  `price_oracle` — *not* from the spot `collateral_amount`, which a crvUSD↔LP trade
  against the AMM could inflate. `half_tvl` is the AMM's equity (`calc_coll_value −
  calc_debt`, == `value_oracle` at equilibrium); both are the YB-position slice, so
  numerator and denominator match. It prices the LP via the twocrypto LP oracle at
  `price_oracle` and slides the AMM along its bonding curve (falling back to raw
  collateral/debt only when the AMM is untradable — and there nothing can manipulate
  it; the Curve pool never reverts, so the crvUSD split stays oracle-based).
- **Sink size** is `sink_pool.totalSupply() * get_virtual_price()` (stableswap vprice
  is not spot-manipulable).
- **Fee conversion** is bounded on *both* legs by `swap_fee_multiplier × pool.fee()`
  (default 1.5× the live dynamic fee): the `LT.withdraw` `min_assets` is the
  `price_oracle`-fair asset value of the shares (`half_tvl · shares/totalSupply /
  price_oracle`, computed inline in `PID._convert_fees` from the oracle's `half_tvl` —
  *not* the `price_scale`-based `value_oracle`, which over-values during imbalance), and
  the asset→crvUSD swap's `min_dy` from `price_oracle`. An on-chain study over ~5
  months (incl. `price_oracle/price_scale` down to 0.62) showed the realizable
  withdrawal stays within ~2.7% of the `price_oracle` fair value, tracking the
  cryptopool's dynamic fee, so `1.5× pool.fee()` covers it with margin (and reverts —
  retry later — only under extreme transient volatility/manipulation).

## Control loop (PID)

`PID.trigger()` converts any held LT fees, then (at most every `min_interval`) steps
the controller. All math is 1e18 fixed point; `dt` is in years.

```
pressure     = max(0, Σ net_pressure(lt)) / H               # via net_pressure_and_tvl(lt) per pool
sink         = sink_pool_TVL / H                            # H = Σ half_tvl(lt)  (AMM equity, x0-based)
error        = pressure − sink                              # coverage gap
integral    += error · dt                  clamped to [0, max_integral]   (anti-windup)
d_pressure   = max(0, d(pressure)/dt)                       # derivative on rising pressure only
target_sink  = clip(feedforward_gain·pressure + kp·error
                    + ki·integral + kd·d_pressure, 0, sink_cap)
offer        = dead_band + target_sink / sink_per_offer     # offered APR as a multiple of market rate
bonus_apr    = (offer − 1) · market_rate
rate         = bonus_apr · staked_value / seconds_per_year  # crvUSD/sec set on the FastGauge
```

![control block diagram](./pics/incentive_block_diagram.png)

### Default parameters

Tuned offline against historical net pressure (see the report); all DAO-settable.

| Param | Default | Meaning |
|---|---|---|
| `feedforward_gain` | 1.16 | proportional gain on raw pressure |
| `kp`, `ki`, `kd` | 50, 1988, 0.0158 | PID gains on the coverage error (time in years) |
| `max_integral` | 2.93 | integral clamp (anti-windup) |
| `sink_cap` | 22 | clamp on the target sink |
| `dead_band` | 1.6 | offered APR multiple at zero target sink |
| `sink_per_offer` | 0.5 | target sink drawn per unit offer above the dead band |
| `swap_fee_multiplier` | 1.5 | fee-conversion slippage buffer (× pool fee) |
| `min_interval` | 3600 s | minimum spacing between controller steps |

## FastGauge reward streaming

Reuses Curve's V5 extra-reward integral (`integral += pulled·1e18/totalSupply`;
`claimable += balance·(integral − integral_for)/1e18`; checkpoint before every
balance change), with one change: rewards are **pulled from the PID at checkpoint**,
capped by the PID's balance/allowance. When the reserve empties, `pulled = 0` and the
effective rate drops to zero **with no reverts** — no `period_finish` needed. Only the
PID can call `set_reward_rate`.

Shares are **1:1** with the staked LP (no virtual offset). Inflation-attack
protection is a *seed-the-market* floor: total supply must be `0` or
`>= MIN_TOTAL_SUPPLY` (default `10 * 1e18`, ~$10) — you can't bootstrap a 1-share
vault, and the last withdrawal must exit fully or leave the floor. To grief a victim
depositing `V` an attacker must donate `> V * MIN_TOTAL_SUPPLY`, so the protection
scales with the seed (~1e19), without locking any permanent dead shares.

## Market rate (sUSDS)

`MarketRateGetter` reads the Sky Savings Rate: `ssr` is a per-second compounding
factor in RAY (`1 + r_per_second`), so the simple APR is `(ssr − RAY) ·
seconds_per_year`, rescaled to 1e18. Returns 0 for `ssr ≤ RAY`. The DAO can swap the
getter for a different source.

## Wiring / deployment

1. Deploy `YBNetPressure`, `MarketRateGetter(sUSDS)`, the sink `FastGauge(lp, crvUSD, dao)`, the `PID(crvUSD, oracle, marketRate, feeDistributor, dao)`, and `FeeSplitter(feeDistributor, pid, fraction, dao)`.
2. `PID.set_pressure_lts([...])`, `PID.set_gauge(fastGauge, sinkPool)` (approves crvUSD pulls), `FastGauge.set_pid(pid)`.
3. Make sure the LT tokens are in the FeeDistributor token set (FeeSplitter/PID read it from there).
4. Point `Factory.set_fee_receiver(feeSplitter)` via the FactoryOwner/DAO path.

## Testing

- `tests/net_pressure/test_incentive_system.py` — unit tests (mocks): MarketRateGetter, FastGauge accrual/split/access/depletion, FeeSplitter split/recover/validation, and the PID step vs a Python reference of the control law.
- `tests/net_pressure/test_market_rate_forked.py` — `MarketRateGetter` vs live sUSDS (set `ETH_RPC_URL`, else `tests_forked/networks.py`; skips if no RPC).
- `tests/lt/test_net_pressure_integration.py` — full stack on a real LT/AMM/cryptopool.
- `tests/lt/test_net_pressure.py` — the net-pressure oracle itself.
