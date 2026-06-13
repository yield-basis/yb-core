# ChainSecurity Audit Response — AMM + LT Hardening & YBLendingOracle

**Audit:** *Code Assessment of the AMM + LT Hardening & YBLendingOracle Smart Contracts* (Draft, 2026-04-21), by ChainSecurity
**Reviewed version:** commit `fb3c7466567822ba3fe9fe587e3ea15afe8a1502`
**Scope:** `contracts/LT.vy`, `contracts/AMM.vy`, `contracts/utils/YBLendingOracle.vy` (delta review; `liboracle.vy` reviewed by a separate team)
**Response date:** 2026-06-13

This document records the Yield Basis team's response to finding **#001**. The
`YBLendingOracleLL.vy` variant (capped-virtual-price oracle) shares the relevant
code path with `YBLendingOracle.vy` and is treated identically throughout.

---

## #001 — Low Gas Attack on `get_state()` Call — **Fixed**

### The finding

`YBLendingOracle._price` (and `YBLendingOracleLL._price_in_asset`) calls
`amm.get_state()` via `raw_call(..., revert_on_failure=False)` and, on failure,
silently falls back to a balance-based valuation that returns a **different** price.
The failure is *intended* to trigger on a genuine revert — `get_x0()`'s quadratic
discriminant goes negative when the AMM is too imbalanced (`d/cv ≥ 9/16`). But a
swallowed call cannot distinguish that legitimate revert from an **out-of-gas**
condition. Under EIP-150's 63/64 rule a caller can forward just enough gas that the
nested `get_state()` runs out of gas while the calling frame retains its 1/64 and
finishes on the fallback branch — returning the alternate price even though the AMM
is healthy. Depending on the consumer, this is a price-manipulation lever.

### Is it exploitable today? Measurements

A silent path-flip is only feasible when

```
cost(get_state) > 63 × cost(fallback-tail)
```

because the calling frame keeps only 1/64 of the gas-at-call, and the entire
fallback branch (the `else` block plus the per-token scaling and return) must
complete within that 1/64.

We measured both sides on a mainnet fork (titanoboa), across all markets and across
market regimes:

**`get_state()` gas, all markets (chain head, block 25310458):**

| markets | warm `get_state()` gas |
|---------|------------------------|
| 1–10 | ~48,000 – 55,500 |
| 0 | 143k — cold first-touch artifact of the first AMM call in a fresh fork, not representative |

**`get_state()` gas at peak imbalance** (markets 3–6, early February 2026, when
`d/cv` reached ~0.53–0.537 — stressed, near `MAX_SAFE = 8.5/16 = 0.53125`, but below
the `CRITICAL = 9/16 = 0.5625` threshold where `get_x0()` reverts):

| date | `d/cv` (m6) | warm `get_state()` gas |
|------|-------------|------------------------|
| Feb 05 | 0.5357 | ~26,100 |
| Feb 06 | 0.5368 | ~35,000 |
| Feb 07 | 0.5373 | ~36,200 |

Key observation: `get_state()` does **not** blow up as the AMM approaches the
imbalance boundary — at peak stress it is actually *cheaper* (~26–36k) than in calm
markets (~50–55k), because cost is dominated by fixed storage reads and the quadratic
solve, not by anything that scales with imbalance.

**Fallback tail:** the `else` branch performs four external reads
(`collateral_amount`, `get_debt`, `liquidity`, `totalSupply`) plus arithmetic.
Measured **warm** (the attacker-optimal case — access lists are transaction-scoped,
so an attacker can pre-warm every slot before calling the oracle) this is **~2,090
gas**.

**Headroom:** with warm numbers, `63 × 2,090 / 36,225 ≈ 3.6×`. That is the
attacker-optimal margin: `get_state()` would have to become ~3.6× more expensive
(or the fallback tail ~3.6× cheaper) before any gas budget could starve `get_state()`
while leaving enough for the fallback.

**Behavioral confirmation:** we built an in-EVM attacker contract that pre-warms all
relevant storage and then invokes the oracle through a gas-limited `raw_call`,
sweeping the budget from 20k to 500k across every market. It produced **zero**
silent flips — the oracle always returned the correct price or reverted, never the
fallback value. So the issue is **not exploitable on the deployed code** as it
stands.

> Note on the margin magnitude: the realistic figure is ~3.6×, not the ~25× one
> might get by assuming the fallback's four staticcalls hit *cold* storage. Because
> access lists are transaction-scoped, an attacker can pre-warm those slots, which is
> the case that governs feasibility. 3.6× is comfortably safe but single-digit.

### Why fix it anyway: the safety rests on the gas schedule

The ~3.6× margin is a ratio of two opcode bundles — a compute-leaning `get_state()`
versus a storage/call-leaning fallback tail — and the 63/64 rule is itself a ratio
rule, invariant to any global rescaling. So the margin is governed entirely by the
**relative** gas pricing of compute vs. state access.

That relative pricing is exactly what the upcoming Ethereum upgrade changes. The 2026
**Glamsterdam** upgrade carries a gas repricing via **EIP-7904** (with related work
such as EIP-8037 and EIP-7907). EIP-7904 reprices computational opcodes while
explicitly *excluding* network/state-persistence costs (SLOAD, CALL); the current
draft direction makes compute **cheaper relative to state access**. For this oracle
that direction happens to *improve* the margin (the compute-leaning `get_state()`
gets relatively cheaper than the storage-leaning tail). But the sign is not
guaranteed across the full repricing roadmap, the package is still in flux, and the
broader point stands: **pinning a security property to the EVM gas schedule, on an
immutable contract, right as Ethereum begins periodically rewriting that schedule, is
fragile.** A later state-access repricing, or a change to the nested oracle call's
cost, could move it the other way, and there would be no signal — the contract cannot
be upgraded.

The fix removes the dependency entirely: it makes the attack surface
**schedule-independent**, so no repricing in any direction can reintroduce the flip.
Given the oracle is not yet deployed, locking this in now is essentially free.

### The fix

A ratio-based guard around the swallowed call, in both oracles:

```vyper
gas_before: uint256 = msg.gas
success, response = raw_call(
    amm.address, method_id("get_state()"),
    max_outsize=96, revert_on_failure=False, is_static_call=True)
if not success and not use_balances:        # LL variant: `if not success:`
    assert msg.gas > gas_before // 16, "GAS"
```

(`YBLendingOracle.vy:179,194`; `YBLendingOracleLL.vy:193,205`. The
`not use_balances` condition skips the guard when the caller has *explicitly*
requested the balance path.)

The guard only executes on the failure branch, so the healthy path (first call
succeeds) is untouched — no change to gas estimation or to normal callers. When the
first call fails it distinguishes the two causes by how much gas survived:

- a **genuine imbalance revert** is cheap and leaves most of `gas_before` → guard
  passes → the oracle proceeds to the (correct) balance fallback. Availability in
  stressed markets is preserved.
- a **gas-starvation OOG** consumed its whole ~63/64 allotment and left only ~1/64 of
  `gas_before` → guard fails → the whole call reverts. The attacker gets a revert,
  never a silent flip.

### Why `msg.gas // 16`

The first `get_state()` call is made with no explicit gas parameter, so EIP-150
forwards all-but-one-64th of the gas available at the call. Therefore:

- If `get_state()` **OOGs**, it consumes essentially that entire 63/64 forward, and
  the calling frame is left with only the retained **~`gas_before / 64`**.
- If `get_state()` **reverts genuinely** (negative discriminant), it returns early and
  cheaply, leaving **~`gas_before − cost(get_state)`**, i.e. nearly all of `gas_before`.

The threshold must sit strictly between these two residuals. `gas_before // 16` is
**4× the ~`gas_before/64` OOG residual**, which:

- comfortably rejects the OOG case (a 4× gap absorbs the small approximations — that
  `gas_before` is captured a few opcodes before the actual CALL, and the call-setup
  overhead — so an OOG can never leave more than the threshold); and
- passes the genuine-revert case as long as the caller supplied
  `gas_before > (16/15) × cost(get_state) ≈ 1.07 × cost(get_state)` — a trivial bar
  for any real consumer, since a caller must already provide well above `get_state`'s
  cost for the *success* path to work at all.

Crucially the bound is a **ratio of `msg.gas` to `gas_before`**, not an absolute gas
constant. A global rescale multiplies both sides equally and cancels, so the guard
behaves identically before and after Glamsterdam (or any future repricing) — there is
no magic number to re-tune and no way for it to drift out of calibration. This is the
deliberate reason we did **not** reuse the absolute-constant pattern from
`LT._checkpoint_gauge` (`assert msg.gas >= 200_000`): a hardcoded gas floor in an
immutable contract bakes in today's schedule and would mis-calibrate under a
repricing, and it would also revert legitimate low-gas fallback callers in exactly the
imbalanced markets where the oracle matters most.

The one residual edge: a caller supplying gas only marginally above `get_state`'s own
cost into a genuinely-imbalanced market could trip the `"GAS"` revert. This is
acceptable — such a caller is under-funding the oracle call and would be at risk on
the success path too.

### Tests

- `tests_forked/test_oracle_gas_griefing.py` — mounts the in-EVM pre-warming attacker
  across a gas sweep on every market and asserts no silent flip is reachable
  (`test_no_silent_fallback_flip`); records the quantitative headroom and floors it
  (`test_get_state_gas_margin`). Passes pre-fix (pinning the ~3.6× margin) and post-fix.
- `tests/test_oracle_gas_guard.py` — hermetic unit test of the guard against
  controlled targets: a cheap genuine revert passes the guard (fallback proceeds), a
  gas-burner that always OOGs is always blocked (reverts, never a silent failure), and
  the threshold behaves identically across 200k / 2M / 20M gas budgets
  (ratio-invariance).
