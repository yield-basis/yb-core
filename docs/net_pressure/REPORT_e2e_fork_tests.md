# End-to-end fork tests of the net-pressure system

These are integration tests that run the **whole net-pressure stack against live mainnet
state** (a `boa.fork`), rather than against unit mocks. They validate the on-chain
*plumbing* — realizing LT admin fees, splitting them, converting to a crvUSD reserve, and
streaming from the gauge — and they exercise the **connection gate** and the controller's
behaviour on real net pressure. The closed-loop *depositor* dynamics (how the sink actually fills in
response to the offer) are out of scope here and live in the simulations behind
[`REPORT_dynamic_incentives.md`](./REPORT_dynamic_incentives.md); these tests hold the sink
fixed and check what the controller *outputs*.

Tests:
- [`tests_forked/test_net_pressure_e2e.py`](../../tests_forked/test_net_pressure_e2e.py) — the full fee→reserve→stream flow on a block chosen for having real pending fees.
- [`tests_forked/test_net_pressure_balanced.py`](../../tests_forked/test_net_pressure_balanced.py) — a single balanced pool (WBTC), the clean-start rate on a healthy pool.

Run them with `uv run pytest -vv tests_forked/test_net_pressure_e2e.py tests_forked/test_net_pressure_balanced.py` (fork-based tests take no `-n`/`--forked`).

## Picking a block with real pending fees

The FeeSplitter's flow only moves tokens if an LT actually has admin fees to realize
(`withdraw_admin_fees()` mints LT shares to the `fee_receiver`). To exercise it on real
data we scanned the **last 3 days, 10 000 sampled blocks**, one Multicall3 `eth_call` per
block batching `totalSupply()` + the public `liquidity()` buffer for markets 7–10, and
computed the `to_mint` each block would realize.

Result: at **block 25473385**, market 7 (`yb-WBTC`) had **~0.069 LT shares (~$6.9k of WBTC)**
pending — the 3-day peak — and markets 8–10 had **none**. That is the block the E2E test
pins.

## Test 1 — full flow (`test_net_pressure_e2e.py`)

At the fee-peak block, deploy `YBNetPressure` / `MarketRateGetter` / `FastGauge` / `PID` /
`FeeSplitter` against the **real, live FeeDistributor** (`contracts/dao/FeeDistributor.vy`
at the Factory's current `fee_receiver`), install the FeeSplitter as the Factory's
`fee_receiver`, stake crvUSD/pyUSD sink LP in the gauge, and run a **single
`FeeSplitter.trigger()`**. The controller aggregates the net pressure of **all four markets
7–10** (`pressure_lts`); at this block markets 8/9 are imbalanced, so the aggregate signal
is **P ≈ 12–13%** of half-TVL.

> **Reading the FeeDistributor's token set.** This surfaced a real bug: the FeeSplitter/PID
> originally called `token_sets(setId) -> DynArray`, but the FeeDistributor stores the sets
> as a `DynArray[IERC20, MAX_TOKENS][N]`, so its only accessor is the *element* getter
> `token_sets(setId, i) -> token` (no whole-array getter, no length). Calling the one-arg
> form reverts against the real contract. Both were fixed to **enumerate the set by index
> until the bounds check reverts**, which is what let this test drop the mock and use the
> live FeeDistributor.

Checked, against a ground-truth `realized` amount measured in a rolled-back `anchor()`:

1. **Split is exact** — the FeeDistributor receives `realized·(1−frac)`, the PID receives `realized·frac` and converts it, the FeeSplitter is left empty, `fill_epochs()` is poked.
2. **Conversion** — the PID's crvUSD reserve grows (LT shares → asset → crvUSD swap).
3. **Rate** — matches the gains (below).
4. **Streaming** — a staker accrues and claims crvUSD, pulled from the PID reserve.

Two parameter sets, same flow:

| variant | gains | outcome |
|---|---|---|
| `default_zero_rate` | contract defaults | rate **0** — the sink is over-provisioned so the controller wants no more sink |
| `wrong_positive_rate` | `kp=0`, `dead_band=3×` | rate **> 0** — the controller ignores existing coverage and streams a bonus |

The zero-rate case needs a **deliberately over-provisioned sink** (far larger than the ~$2M
live crvUSD/pyUSD pool): the YB markets are ~$92M aggregate half-TVL, so only a sink that
dwarfs the net pressure makes the controller want nothing. This is a test fixture, not a
realistic sink — realistically a small sink *can't* cover these markets and the default
gains correctly pay a bonus.

Two implementation notes that bit us and are worth remembering:
- Fabricate the sink with `boa.deal(..., adjust_supply=False)`. Bumping `totalSupply` would dilute the pool's `get_virtual_price` (`≈ D/supply`) and silently mis-value the sink.
- Each parametrization re-forks (function-scoped fixture) so both see the intact pending fees.

## The connection gate — no cold start at all

`PID.trigger()` runs the controller **only while connected** — i.e. when the Factory's
`fee_receiver` is a contract whose `pid()` points back at this PID (our FeeSplitter). This
directly fixes the pre-connection reality: in production the pool/PID exist **3–4 days
before** the DAO installs the FeeSplitter as `fee_receiver`, and `trigger()` is permissionless.

- **Before connection:** every `trigger()` is a no-op that only keeps the clock fresh — it
  never integrates, so the integral cannot wind up over that dead window, and the rate stays 0.
- **At connection:** the controller starts from a **clean slate** — `last_ts` reset to now
  (no big-`dt` jump), `prev_pressure = P` (no `0 → P` derivative kick), `integral = 0`. So
  there is **no cold-start transient** and **no windup**; the rate is the settled value from
  the first connected trigger.

This obviates the earlier "warm up first" dance and removes the integral-windup / overspend
risk of running the controller against an empty reserve.

## Test 2 — a balanced pool (`test_net_pressure_balanced.py`)

**WBTC alone** at a recent head (block 25483052), net pressure only **~1.05%** of half-TVL —
a healthy, unstressed pool. Triggering every 6h with two sink sizes confirms the clean-start
behaviour:

- **barely covers P** (S ≈ 1.36%): a **small, steady positive rate** (the feedforward
  residual) from the first trigger — no bump to decay. `d_pressure ≈ 0`, `integral == 0`.
- **over-provisioned** (S ≈ 9%): **rate 0** throughout — the coverage term wants no sink.

So a healthy pool costs almost nothing, and the integral never leans toward `sink_cap`
because the coverage error is ≤ 0.

## Test 3 — the gate (`test_net_pressure_e2e.py::test_controller_gated_off_until_connected`)

Single market 7, barely-covering sink. Pre-connection, `pid.trigger()` is called every 12h
for ~4 days; then the FeeSplitter is installed and the fee claimed:

- through the window: `active == False`, `integral == 0`, `d_pressure == 0`, `reward_rate == 0` — **no windup**;
- the pending admin fee is **untouched** (still claimable) — the controller never converted anything;
- on connection: `active == True`, `integral == 0`, `d_pressure == 0` (**clean start, no kick**), the fee splits/converts, and the rate is the settled positive value.

### Spend consistency

The settled offer of ~1.3× on a barely-covered balanced pool corresponds to
`spend = (x−1)·m·S ≈ 0.3 · 3.5% · 1.36% ≈ 0.015%/yr of half-TVL` — i.e. **a healthy pool
costs almost nothing**. That matches the model: the headline **0.1–0.15%/yr** of YB TVL in
[`REPORT_dynamic_incentives.md`](./REPORT_dynamic_incentives.md) is the time-average
dominated by the *stress* episodes, not the calm baseline. With the gate the controller
starts clean at connection, so there is no cold-start bump on top of that steady spend.

## Caveats

- **No depositor plant.** The staked sink is fixed; these tests do not model how the sink fills/drains in response to the offer. So they cannot reproduce the model's *settled* spend under closed-loop dynamics — with a fixed under-covering sink the integral would wind up to `sink_cap`. That closed loop is validated by the simulations, not here.
- **Block-pinned.** Both blocks are pinned for reproducibility; the numbers above are point-in-time snapshots of live state.
