# End-to-end fork tests of the net-pressure system

These are integration tests that run the **whole net-pressure stack against live mainnet
state** (a `boa.fork`), rather than against unit mocks. They validate the on-chain
*plumbing* — realizing LT admin fees, splitting them, converting to a crvUSD reserve, and
streaming from the gauge — and they characterize the **controller's cold-start behaviour**
on real net pressure. The closed-loop *depositor* dynamics (how the sink actually fills in
response to the offer) are out of scope here and live in the simulations behind
[`REPORT_dynamic_incentives.md`](./REPORT_dynamic_incentives.md); these tests hold the sink
fixed and check what the controller *outputs*.

Tests:
- [`tests_forked/test_net_pressure_e2e.py`](../../tests_forked/test_net_pressure_e2e.py) — the full fee→reserve→stream flow on a block chosen for having real pending fees.
- [`tests_forked/test_net_pressure_balanced.py`](../../tests_forked/test_net_pressure_balanced.py) — a single balanced pool (WBTC), characterizing the cold-start derivative transient.

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

## Test 2 — a balanced pool, and the cold-start transient (`test_net_pressure_balanced.py`)

A more representative case: **WBTC alone** at a recent head (block 25483052), where its net
pressure is only **~1.05%** of half-TVL — a healthy, unstressed pool.

The point is the **cold start**. On the first-ever trigger `prev_pressure` steps `0 → P`, so
the (6h-filtered) derivative spikes and briefly lifts the offer; over ~`Tf` it decays and
the offer settles to what the feedforward/proportional terms alone sustain. Triggering every
6h with a sink that *barely* covers P (S ≈ 1.36%):

| trigger (h) | d_pressure | offer (×market) | rate (wei/s) |
|---:|---:|---:|---:|
| 0 (cold) | 8.47 | **2.14×** | 3.85e14 |
| 6 | 4.23 | 1.72× | 2.45e14 |
| 12 | 2.12 | 1.51× | 1.74e14 |
| 18 | 1.06 | 1.41× | 1.39e14 |
| 24 | 0.53 | 1.36× | 1.22e14 |
| 30 | 0.27 | **1.33×** | 1.13e14 |

With an **over-provisioned** sink (S ≈ 9%) instead, the rate is **0 at every trigger** — the
coverage term dominates even the cold-start kick.

What this establishes:

- **The derivative kick is a pure transient.** It halves every `Tf = 6h` (the `0→P` step response of the Åström filter) and contributes nothing once calmed. It front-loads the offer to fill the sink faster; it is not a standing cost.
- **What's left is the feedforward residual.** On a barely-covered balanced pool the offer settles to **~1.3×**; over-covered it settles to **0**.
- **The integral never winds up** (`integral == 0` asserted). Because the sink covers the small pressure, the coverage error is ≤ 0, so nothing drives the integral toward `sink_cap`.

## Test 3 — warm-up before connection removes the cold start (`test_net_pressure_e2e.py`)

In production the pool/PID typically exist **3–4 days before** the FeeSplitter is installed
as the Factory `fee_receiver`. `trigger()` is permissionless, so the controller can be run
during that window — and doing so removes the cold-start artifact entirely.

The test warms the PID with `pid.trigger()` every 12h for ~4 days *before* connecting the
splitter. Crucially it uses **`pid.trigger()`, not `fs.trigger()`**: the former steps the
controller and converts only the PID's own (zero) LT balance, so it does **not** call
`withdraw_admin_fees()` and the pending fee is preserved for the real claim.

Warm-up (single market 7, barely-covered sink so the derivative shows in the rate):

| t (h) | d_pressure | rate (wei/s) |
|---:|---:|---:|
| 0 (cold) | 16.50 | 1.12e15 |
| 12 | 5.50 | 6.33e14 |
| 24 | 1.83 | 4.75e14 |
| 48 | 0.20 | 4.13e14 |
| 72 | 0.023 | 4.15e14 |
| 96 | 0.0025 | 4.23e14 |

Then the splitter is connected (`set_fee_receiver`) and `fs.trigger()` claims the fee:

- the pending fee is **still claimable** (`~0.0689` shares — `pid.trigger` didn't touch it), and splits/converts normally (~2125 crvUSD to the reserve);
- `d_pressure ≈ 0.0008` at connection — the derivative artifact is **gone**;
- the rate at connection is the **settled** value (`~4.3e14`), strictly below the day-0 cold rate (`1.12e15`) — no cold-start bump.

So warming the controller during the pre-connection window means it starts *connected* in
steady state. The cold-start derivative spike the other tests exhibit is a one-time artifact
that a few pre-connection triggers absorb (it halves every `Tf = 6h`, so after 3–4 days it is
~10⁻⁵ of the kick). Staking the sink before warm-up also keeps the integral at 0, so the
controller reaches true steady state with no transient of any kind.

### Spend consistency

The settled offer of ~1.3× on a balanced pool corresponds to
`spend = (x−1)·m·S ≈ 0.3 · 3.5% · 1.36% ≈ 0.015%/yr of half-TVL` — i.e. **a healthy pool
costs almost nothing**. That is exactly the model's behaviour: the headline **0.1–0.15%/yr**
of YB TVL in [`REPORT_dynamic_incentives.md`](./REPORT_dynamic_incentives.md) is the
time-average dominated by the *stress* episodes, not the calm baseline. The cold-start bump
is a bounded transient, not a standing cost, so starting the controller at an existing
imbalance does not change the sustained spend.

## Caveats

- **No depositor plant.** The staked sink is fixed; these tests do not model how the sink fills/drains in response to the offer. So they cannot reproduce the model's *settled* spend under closed-loop dynamics — with a fixed under-covering sink the integral would wind up to `sink_cap`. That closed loop is validated by the simulations, not here.
- **Block-pinned.** Both blocks are pinned for reproducibility; the numbers above are point-in-time snapshots of live state.
