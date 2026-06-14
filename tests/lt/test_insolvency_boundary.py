"""
Insolvency / return-0 boundary of a 2x YB position as a function of pool A.

The oracle's collateral valuation at the EMA price is collateral_value(p) = 2*debt *
portfolio_value(A, p), where p = price_oracle/price_scale (a 2x position has
collateral_value = 2*debt at the reference p=1). Hence, assuming p_real = p_oracle:

  * balance-insolvent (collateral_value < debt)   <=>  portfolio_value(A, p) < 1/2
  * oracle returns 0 (success-branch equity wiped) <=>  portfolio_value(A, p) < 9/16

Both are pure functions of A and p. The Twocrypto pool clamps p >= price_scale/2, i.e.
p >= 0.5. This test tabulates the boundary p for each, and pins the structural facts:
  - the balances-insolvency boundary is always BELOW 0.5 (the clamp floor), so a position
    is never balance-insolvent at the oracle price for any A -- the clamp guarantees it;
  - the return-0 boundary only rises above 0.5 (becomes reachable) for A_true >~ 12.
"""
WAD = 10**18
INSOLVENT = WAD // 2          # portfolio_value = 0.5
RETURN0 = 9 * WAD // 16       # portfolio_value = 9/16
CLAMP = WAD // 2              # pool clamps p = price_oracle/price_scale >= 0.5

A_TRUES = [1.25, 2.5, 4.5, 10, 14, 20, 30, 50, 100, 1000, 100000]


def _solve_p(lp, A_raw, target):
    # smallest p in [0.01, 1] with portfolio_value(A_raw, p) >= target (pv increasing in p)
    lo, hi = WAD // 100, WAD
    if lp.portfolio_value(A_raw, lo) >= target:
        return None  # boundary below MIN_P -> effectively unreachable
    for _ in range(60):
        mid = (lo + hi) // 2
        if lp.portfolio_value(A_raw, mid) < target:
            lo = mid
        else:
            hi = mid
    return hi


def test_insolvency_and_return0_boundary_table(lp_oracle_2):
    lp = lp_oracle_2

    print(f"\n{'A_true':>8} {'A_raw':>10} {'cover@p=0.5':>11} {'p_insolvent':>11} {'p_return0':>11}  reachable?")
    for A_true in A_TRUES:
        A_raw = int(A_true * 10**4)
        cover = 2 * lp.portfolio_value(A_raw, CLAMP) / WAD     # collateral_value/debt at clamp floor
        p_ins = _solve_p(lp, A_raw, INSOLVENT)
        p_r0 = _solve_p(lp, A_raw, RETURN0)
        fp = lambda p: ("<0.01" if p is None else f"{p/WAD:.4f}")
        r0_reach = (p_r0 is not None and p_r0 > CLAMP)
        print(f"{A_true:>8} {A_raw:>10} {cover:>11.4f} {fp(p_ins):>11} {fp(p_r0):>11}  "
              f"return0 {'reachable' if r0_reach else 'unreachable'}")

        # Balances-insolvency boundary is always below the clamp floor -> never insolvent at
        # the oracle price; equivalently coverage at the clamp floor is >= 1.
        assert cover >= 1.0, f"A_true={A_true}: coverage at clamp {cover} < 1"
        assert p_ins is None or p_ins < CLAMP, f"A_true={A_true}: insolvency boundary {p_ins} >= clamp"

    # Deployed pools (A_true <= 4.5): even the return-0 boundary is below the clamp -> the
    # oracle never returns 0. High A (>=14): return-0 is reachable inside the clamp.
    assert _solve_p(lp, int(4.5 * 10**4), RETURN0) < CLAMP
    assert _solve_p(lp, int(20 * 10**4), RETURN0) > CLAMP
