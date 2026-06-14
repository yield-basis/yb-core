# ChainSecurity Audit Response — AMM + LT Hardening & YBLendingOracle

**Audit:** *Code Assessment of the AMM + LT Hardening & YBLendingOracle Smart Contracts* (Draft, 2026-04-21), by ChainSecurity
**Reviewed version:** commit `fb3c7466567822ba3fe9fe587e3ea15afe8a1502`
**Scope:** `contracts/LT.vy`, `contracts/AMM.vy`, `contracts/utils/YBLendingOracle.vy` (delta review; `liboracle.vy` reviewed by a separate team)
**Response date:** 2026-06-13

This document records the Yield Basis team's response to findings **#001**–**#005**.
The `YBLendingOracleLL.vy` variant (capped-virtual-price oracle) shares the relevant
code paths with `YBLendingOracle.vy` and is treated identically throughout.

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

---

## #002 — YBLendingOracle Reverts on Price Oracle Divergence — **Fixed (returns 0)**

### The finding

In the `get_state()`-success branch, `_price` computes
`isqrt(10**36 * lp_price_oracle // lp_price_ps) * (2L)//(2L-1) - 10**18`, which
underflows (reverts) when `lp_price_oracle / lp_price_ps < (3/4)^2 = 9/16`. As
ChainSecurity notes, this is distinct from the AMM's own `get_x0()` revert:
`get_state()` can succeed while this expression underflows, so `price_in_asset` /
`price_in_usd` revert during a crash — exactly when a lending integrator needs a price.

### What drives the underflow

The ratio is exactly the StableSwap portfolio value. Since
`lp_price_ps = 2·vprice·√price_scale = D/totalSupply` and
`lp_price_oracle = portfolio_value(A, p)·D/totalSupply`,

```
ratio = lp_price_oracle / lp_price_ps = portfolio_value(A, p),   p = price_oracle / price_scale
```

Two structural facts bound it:

- The Twocrypto pool clamps the EMA input to `[price_scale/2, 2·price_scale]`
  (`Twocrypto.vy`: `min(max(last_prices, price_scale/2), 2·price_scale)`), so
  `p ∈ [0.5, 2]`. Hence `ratio ≥ portfolio_value(A, 0.5)`.
- `portfolio_value(A, 0.5)` decreases monotonically in `A` and crosses `9/16` at
  `A_raw ≈ 119k` (`A_true ≈ 12`). So the underflow is reachable only for high-A pools.

Measured `portfolio_value(A, 0.5)` (the ratio floor) per deployed pool:

| markets | A_true | A_raw | portfolio_value(0.5) | vs 9/16 |
|---|---|---|---|---|
| 6 | 1.2 | 12.5k | 0.641 | safe |
| 7–10 | 2.5 | 25k | 0.614 | safe |
| 0–5 | 4.5 | 45k | 0.593 | safe |

All deployed pools use `A_true ≤ 4.5`, so the ratio floors at 0.59–0.64, above `9/16`
— **the underflow is unreachable on every current market.**

### `get_state()` does not revert first

The intuition that the AMM's `get_x0()` revert (`d/cv ≥ 9/16`) precedes the oracle
underflow does not hold: `get_state()` values collateral via `price_scale` (the
`CryptopoolLPOracle`), which is sticky on a one-sided crash, while the underflow is
driven by `price_oracle/price_scale`. The two are governed by different prices and are
decoupled. In a reproduction, `get_state()` stayed solvent through a 50% spot crash
while the EMA fell to the clamp floor.

### Is the underflow region insolvent?

The success-branch (x0) value and the balances value are exactly
`yb_S = equity·(4√ratio − 3)` and `yb_B = equity·(2·ratio − 1)`, so
`yb_B − yb_S = 2·equity·(√ratio − 1)² ≥ 0` — the x0 branch is the more conservative,
and the two agree exactly at `ratio = 1`. The underflow (`yb_S ≤ 0`) begins at
`ratio = 9/16`, where `yb_B` is still `+0.125·equity`. So at the oracle (EMA) price the
position is not yet insolvent when the success branch underflows.

Going further, assuming `p_real = p_oracle`, the balances-insolvency boundary is
`portfolio_value(A, p) = 1/2`, which occurs at `p < 0.5` for every A (since
`portfolio_value(A, 0.5) > 0.5`, approaching 0.5 only as `A → ∞`). Because the pool
clamps `p ≥ 0.5`, the collateral coverage at the oracle price is
`≥ 2·portfolio_value(A, 0.5) ≥ 1.0×` — **the position is never balance-insolvent at the
oracle price, for any A.** Genuine insolvency arises only through `price_oracle`
staleness (`p_real < p_oracle`, the report's SC2): once the true price is below the
clamped EMA, the position can be deeply underwater while `get_state` still values it at
the stale `price_scale`.

Boundary table (2× position; `p = price_oracle/price_scale`; clamp floor `p = 0.5`).
Deployed markets: `A_true = 1.25` (market 6), `2.5` (markets 7–10), `4.5` (markets 0–5).

| A_true | A_raw | coverage at clamp `p=0.5` | `p` insolvent (pv=1/2) | `p` returns 0 (pv=9/16) |
|---|---|---|---|---|
| 1 (min A) | 10k | 1.298× | 0.346 | 0.408 — < clamp, never |
| 1.5 | 15k | 1.268× | 0.364 | 0.426 — < clamp, never |
| 2 | 20k | 1.245× | 0.376 | 0.438 — < clamp, never |
| 2.5 *(mkts 7–10)* | 25k | 1.228× | 0.385 | 0.447 — < clamp, never |
| 3 | 30k | 1.214× | 0.392 | 0.454 — < clamp, never |
| 3.5 | 35k | 1.203× | 0.398 | 0.460 — < clamp, never |
| 4 | 40k | 1.193× | 0.403 | 0.465 — < clamp, never |
| 4.5 *(mkts 0–5)* | 45k | 1.185× | 0.407 | 0.469 — < clamp, never |
| 5 | 50k | 1.178× | 0.411 | 0.473 — < clamp, never |
| 6 | 60k | 1.166× | 0.417 | 0.479 — < clamp, never |
| 7 | 70k | 1.156× | 0.422 | 0.484 — < clamp, never |
| 8 | 80k | 1.148× | 0.426 | 0.489 — < clamp, never |
| 9 | 90k | 1.141× | 0.430 | 0.492 — < clamp, never |
| 10 | 100k | 1.135× | 0.433 | 0.495 — < clamp, never |
| ~12 | ~119k | 1.125× | 0.438 | 0.500 — **crossover** |
| 20 | 200k | 1.100× | 0.450 | 0.513 — reachable |
| 30 | 300k | 1.083× | 0.458 | 0.521 — reachable |
| 100 | 1M | 1.048× | 0.476 | 0.539 — reachable |
| 1000 | 10M | 1.016× | 0.492 | 0.555 — reachable |
| 100000 (max A) | 1e9 | 1.0016× | 0.499 | 0.562 — reachable |

### Resolution

Both branches now **return 0 instead of underflowing**: the success branch returns 0
once the leveraged-equity factor `(2L/(2L-1))·√ratio` drops to `≤ 1` (`ratio ≤ 9/16`),
and the fallback branch returns 0 when collateral value `≤` debt. A reverting price
bricks a lending market's liquidation path; returning 0 lets a (loss-taking) liquidator
clear the position. The same change also resolves the fallback's revert-on-insolvency
(report #004). It is value-only and does not affect normal pricing (forked parity within
1% retained); `0` flows through the per-token scaling cleanly because `lv_total` is the
gross liquidity value and stays positive for any position holding collateral.

### Tests

- `tests/lt/test_lending_oracle_002.py` — the A-gated boundary at the solver level, plus
  a live pool/position crash showing `get_state()` stays solvent and the ratio floors
  just above `9/16` for the deployed A.
- `tests/lt/test_lending_oracle_002_solvency.py` — high-A pool: enters the underflow
  region, shows the position is insolvent at the true price (staleness gap
  `v_true ≤ v_oracle ≤ v_scale`), and asserts `price_in_usd` returns 0 rather than
  reverting.
- `tests/lt/test_lending_oracle_consistency.py` — the `yb_S` vs `yb_B` law
  (`gap = 2·equity·(√ratio − 1)²`; identical at `ratio = 1`).
- `tests/lt/test_insolvency_boundary.py` — the boundary table; pins coverage `≥ 1.0×`
  at the clamp floor for all A, and the `A_true ≈ 12` return-0 crossover.

---

## #003 — Hardcoded Equilibrium Threshold Assumes Leverage of 2 — **Acknowledged (documented; not an issue for YB)**

### The finding

`AMM.exchange()`'s relaxed safety check compares `coll_vs_debt` against a hardcoded
`2 * 10**18` to decide whether the system is above or below equilibrium. The equilibrium
collateral/debt ratio is `LEVERAGE / (LEVERAGE - 10**18)`, which equals 2 only when
`LEVERAGE = 2 * 10**18`. The AMM constructor takes `leverage` as a parameter and asserts
only `leverage > 10**18`, so for any other leverage the hardcoded threshold would
misclassify trades.

### Why this is not an issue for Yield Basis

Leverage is fixed at 2 system-wide, by construction — it is not a per-market parameter:

- **The only way to deploy an AMM is `Factory.add_market`**, which creates the AMM via
  `create_from_blueprint(self.amm_impl, ..., LEVERAGE, ...)`. `Factory.vy` declares
  `LEVERAGE: public(constant(uint256)) = 2 * 10**18` and passes that constant on every
  deployment. No reachable code path constructs an AMM with a different leverage. (A raw
  blueprint is not a usable AMM until instantiated through the Factory.)
- **`YBLendingOracle` assumes the same**: its valuation hardcodes `L = 2` (the
  `(2L)/(2L-1)` leverage factor, the `9/16` boundary, etc.). An AMM with a leverage other
  than 2 would break the oracle regardless of the threshold.

So `2 * 10**18` is correct everywhere it appears, because the system's single, fixed
leverage makes the equilibrium ratio exactly 2. We have **documented this assumption in
`AMM.vy`** — at the constructor (where `leverage` enters) and at the threshold in
`exchange()` — so the coupling is explicit to future readers.

We consider generalizing the threshold to `LEVERAGE / (LEVERAGE - 10**18)` unnecessary:
supporting a non-2 leverage would require generalizing the oracle's `L` (and several
other `L = 2`-derived constants) as well, i.e. a system-wide change well beyond this one
expression. Should YB ever pursue a different leverage, both the threshold and the oracle
must be generalized together.

---

## #004 — Oracle Fallback Reverts on Insolvency Instead of Returning Zero — **Fixed**

### The finding

The fallback (balance-based) branch computed
`collateral * lp_price_oracle * agg_price - debt`, which underflows and reverts when the
oracle-priced collateral value is below the debt. Since the fallback is the recovery path
for when `get_state()` reverts, a genuinely insolvent position could leave *both* the
primary and fallback paths unavailable — bricking a lending integrator's liquidation
exactly when it is needed.

### Resolution

This is the fallback-branch half of the same return-0 change made for #002 (the success
branch was the other half). The branch now computes the collateral value first and only
subtracts the debt when it is covered, returning 0 otherwise:

```vyper
coll_value: uint256 = collateral * lp_price_oracle // 10**18 * agg_price // 10**18
if coll_value > debt:
    yb_oracle = coll_value - debt
# else yb_oracle stays 0
```

So an insolvent position reports 0 instead of reverting, on **both** the success (x0) and
fallback (balances) paths, leaving the position liquidatable. As with #002 this state is
structurally hard to reach at the oracle price — the pool clamps `p ≥ 0.5`, which keeps
the EMA-priced collateral coverage above 1.0× for the deployed low-A pools — so the change
is fail-safe insurance for high-A / stale-price states rather than a change to normal
behaviour. It is exercised by the same return-0 mechanism asserted in
`tests/lt/test_lending_oracle_002_solvency.py` (`price_in_usd` returns 0, never reverts).

---

## #005 — Stale Accounting Causes Systematic Price Deviation — **Fixed**

### The finding

At the reviewed commit (`fb3c746`), `_price` read the stored `lt.liquidity()` and
`lt.totalSupply()` directly and used them to scale the per-token price, without
replicating `LT._calculate_values()`'s between-checkpoint drift. Two effects accumulate
between checkpoints — admin-fee accrual (pushes the price up) and the missing supply burn
(pushes it down) — producing a deviation that scales with staking ratio and accrued fees,
up to ~9.6% at 99% staking in the report's example.

### Resolution

Fixed after the review, in commit `45949eb`, by adding `_calculate_fresh_lv()`, which
replicates `LT._calculate_values()` to recompute the up-to-date liquidity total, admin-fee
balance (`f_a` → `admin`), and token supply including the supply burn (`token_reduction` →
`supply_tokens`). The normal pricing path (`get_state()` success) now scales by these fresh
values rather than the stored cache:

```vyper
lv_total, lv_admin, lt_supply = self._calculate_fresh_lv(lt, p_o, amm_value)
```

so the reported price tracks the LT's effective post-checkpoint value, eliminating the
staking-ratio- and fee-dependent deviation. The forked oracle test
(`tests_forked/test_lending_oracle.py`) corroborates this end-to-end: `price_in_asset`
agrees with the LT's `preview_withdraw` within 1% across markets.

### Residual

The fallback branch (entered only when `get_state()` reverts — deep AMM imbalance) still
reads the stored `lt.liquidity()` / `lt.totalSupply()`, because the fresh calculation needs
the `x0`-derived `amm_value` that `get_state()` provides and that is unavailable on that
path. The drift there is bounded and the path is rare; in that regime the position is also
near the insolvency boundary and frequently prices to 0 (see #002/#004).
