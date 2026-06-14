"""
ChainSecurity #002 — when (if ever) does YBLendingOracle's success branch underflow?

The oracle computes, in the get_state()-success branch:
    isqrt(10**36 * lp_price_oracle // lp_price_ps) * (2L)//(2L-1) - 10**18
which underflows (reverts) exactly when  lp_price_oracle / lp_price_ps < (3/4)^2 = 9/16.

Two facts pin this down:

1. lp_price_ps = 2 * vprice * sqrt(price_scale) ~= D / totalSupply, and
   lp_price_oracle = portfolio_value(A, p) * D / totalSupply, so the driving ratio
   is *exactly* portfolio_value(A, p), the normalised (D=1) StableSwap portfolio value
   at marginal price p = price_oracle / price_scale.

2. The Twocrypto pool clamps the EMA input to [price_scale/2, 2*price_scale]
   (Twocrypto.vy: `min(max(last_prices, price_scale/2), 2*price_scale)`), so
   p = price_oracle/price_scale is structurally confined to [0.5, 2].

Hence the ratio can never go below portfolio_value(A, 0.5). Whether that floor breaches
9/16 depends ONLY on the pool's amplification A:
  - low A  (deployed fxswap pools, A_true ~1-5): portfolio_value(0.5) ~0.59-0.64 -> safe.
  - high A (A_true >~ 14, A_raw >~ 137k):        portfolio_value(0.5) < 0.5625 -> reachable.

So #002 is real but A-gated, and (contrary to the "get_state reverts first" intuition)
get_state() does NOT have to revert before the underflow: get_state values collateral via
the sticky price_scale and stays solvent while p drifts to the clamp floor.
"""
import boa


UNDERFLOW = 9 * 10**18 // 16  # 0.5625e18

# Deployed fxswap pools (mainnet, June 2026): pool A in {25000, 50000, 90000} ->
# A_raw = A_pool * 1e4 // (2 * 1e4) = A_pool // 2  in {12500, 25000, 45000}.
DEPLOYED_A_RAW = [12_500, 25_000, 45_000]


def _ok(fn, *a):
    try:
        return True, fn(*a)
    except Exception as e:
        return False, str(e)


def test_underflow_is_A_gated_at_the_price_clamp(lp_oracle_2):
    """portfolio_value(A, 0.5) — the ratio floor — stays above 9/16 only for low A."""
    lp = lp_oracle_2
    half = 10**18 // 2

    # Deployed pools: the floor stays comfortably above the 9/16 underflow threshold.
    for A_raw in DEPLOYED_A_RAW:
        pv = lp.portfolio_value(A_raw, half)
        assert pv > UNDERFLOW, f"A_raw={A_raw}: portfolio_value(0.5)={pv} <= {UNDERFLOW}"

    # ...but it is monotonically decreasing in A and DOES breach 9/16 for high-A pools,
    # so #002 is a genuine (latent) revert, gated on pool amplification.
    assert lp.portfolio_value(45_000, half) > UNDERFLOW    # deployed max A -> safe
    assert lp.portfolio_value(300_000, half) < UNDERFLOW   # A_true~30 -> reachable

    # Locate the crossover (A_raw where portfolio_value(0.5) == 9/16) for the record.
    lo, hi = 45_000, 300_000
    for _ in range(40):
        mid = (lo + hi) // 2
        if lp.portfolio_value(mid, half) > UNDERFLOW:
            lo = mid
        else:
            hi = mid
    print(f"\nportfolio_value(0.5) crosses 9/16 at A_raw ~= {hi} (A_true ~= {hi/10_000:.1f})")


def test_getstate_does_not_revert_before_underflow(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ratio_probe, lending_oracle,
):
    """
    Live pool/position (A_true=10, just below the crossover): crash the spot price to the
    EMA clamp floor and confirm (a) the ratio bottoms just ABOVE 9/16 so the oracle never
    underflows, and (b) get_state() stays solvent throughout — it does not revert first.
    """
    probe = ratio_probe
    oracle = lending_oracle

    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)

    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, p * 10**18 // 10**18, 0)
        yb_lt.set_rate(0)

    dumper = accounts[3]
    collateral_token._mint_for_testing(dumper, 2000 * 10**18)
    with boa.env.prank(dumper):
        collateral_token.approve(cryptopool.address, 2**256 - 1)

    min_ratio = 10**18
    getstate_always_ok = True
    underflow_while_getstate_ok = []
    for step in range(30):
        gs_ok, _ = _ok(yb_amm.get_state)
        ratio = probe.ratio_e18(cryptopool.address)
        getstate_always_ok &= gs_ok
        min_ratio = min(min_ratio, ratio)
        if gs_ok and ratio < UNDERFLOW:
            underflow_while_getstate_ok.append((step, ratio))
        with boa.env.prank(dumper):
            ok, _ = _ok(cryptopool.exchange, 1, 0, (10 + 4 * step) * 10**18, 0)
        if not ok:
            break
        boa.env.time_travel(3600)

    print(f"\nmin ratio reached = {min_ratio/1e18:.4f} (threshold 9/16 = {UNDERFLOW/1e18:.4f}); "
          f"get_state always ok = {getstate_always_ok}")

    # The ratio floors just above 9/16 (clamp floor p=0.5, A_true=10 -> ~0.567), so no underflow,
    # and get_state never reverted first. Both points of the finding.
    assert not underflow_while_getstate_ok
    assert getstate_always_ok
    assert UNDERFLOW < min_ratio < 57 * 10**16   # ~0.567: pinned just above the threshold
