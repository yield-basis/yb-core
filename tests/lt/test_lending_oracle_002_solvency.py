"""
ChainSecurity #002 follow-up: in the region where the YBLendingOracle success branch
underflows (ratio = portfolio_value(A,p) < 9/16) but get_state() still works, is the
position's balances-based value above or below the debt?

Needs a high-A pool (A_true ~ 30, above the ~11.9 crossover) so the underflow region is
actually reachable at the price_oracle clamp floor (p = price_scale/2). We then read the
position's collateral and debt straight from the AMM and value the collateral two ways:
  - EMA / portfolio-value LP price (lp_price_oracle) -> what the fallback branch would use
  - spot / price_scale LP price   (lp_price_ps)      -> what get_state / the AMM sees
and compare each to the debt.
"""
from pathlib import Path

import boa
from eth.constants import ZERO_ADDRESS


TWOCRYPTO_DIR = "contracts/twocrypto_pool/contracts/main"
UNDERFLOW = 9 * 10**18 // 16  # 0.5625e18


def _ok(fn, *a):
    try:
        return True, fn(*a)
    except Exception as e:
        return False, str(e)


def _high_a_pool(stablecoin, collateral_token, admin, accounts, A_pool):
    code = Path(f"{TWOCRYPTO_DIR}/Twocrypto.vy").read_text()
    with boa.env.prank(admin):
        math_impl = boa.load(f"{TWOCRYPTO_DIR}/StableswapMath.vy")
        views_impl = boa.load(f"{TWOCRYPTO_DIR}/TwocryptoView.vy")
        code = code.replace("MATH = Math(empty(address))", f"MATH = Math({math_impl.address})", 1)
        code = code.replace("VIEW = Views(empty(address))", f"VIEW = Views({views_impl.address})", 1)
        amm_impl = boa.loads_partial(code).deploy_as_blueprint()

        tf = boa.load(f"{TWOCRYPTO_DIR}/TwocryptoFactory.vy")
        tf.initialise_ownership(admin, admin)
        tf.set_pool_implementation(amm_impl, 0)
        tf.set_gauge_implementation(ZERO_ADDRESS.hex())
        tf.set_views_implementation(views_impl)
        tf.set_math_implementation(math_impl)

        pool_i = boa.load_partial(f"{TWOCRYPTO_DIR}/Twocrypto.vy")
        pool = pool_i.at(tf.deploy_pool(
            "HighA", "HiA", [stablecoin.address, collateral_token.address], 0,
            A_pool, int(1e-5 * 1e18), int(0.0025 * 1e10), int(0.0045 * 1e10), int(0.01 * 1e18),
            int(0.0001 / 100 * 10**18), int(10 / 100 * 10**18), 600, 100_000 * 10**18))
        pool.set_fee_parameters(pool.reserved_profit_fraction(), 0)
        for addr in accounts + [admin]:
            with boa.env.prank(addr):
                stablecoin.approve(pool.address, 2**256 - 1)
                collateral_token.approve(pool.address, 2**256 - 1)
    return pool


def test_underflow_region_is_practically_insolvent(
    stablecoin, collateral_token, admin, accounts,
    factory, amm_interface, lt_interface, dummy_gc, mock_agg, ratio_probe, lending_oracle,
):
    probe = ratio_probe
    oracle = lending_oracle
    pool = _high_a_pool(stablecoin, collateral_token, admin, accounts, A_pool=600_000)  # A_true=30

    # Add a YB market on the high-A pool.
    with boa.env.prank(admin):
        market = factory.add_market(pool.address, int(0.007e18), int(0.1e18 / (365 * 86400)), 0)
        dummy_gc.add_gauge(market.staker)
    lt = lt_interface.at(market.lt)
    amm = amm_interface.at(market.amm)

    with boa.env.prank(admin):
        lt.allocate_stablecoins(10**30)
        for addr in accounts + [admin]:
            with boa.env.prank(addr):
                stablecoin.approve(lt.address, 2**256 - 1)
                collateral_token.approve(lt.address, 2**256 - 1)
                pool.approve(amm.address, 2**256 - 1)
                stablecoin.approve(amm.address, 2**256 - 1)

    # Seed pool depth, open a leveraged position.
    stablecoin._mint_for_testing(admin, 100 * 100_000 * 10**18)
    collateral_token._mint_for_testing(admin, 100 * 10**18)
    with boa.env.prank(admin):
        pool.add_liquidity([100 * 100_000 * 10**18, 100 * 10**18], 0)
        p = pool.price_oracle()
        collateral_token._mint_for_testing(admin, 10**18)
        lt.deposit(10**18, p * 10**18 // 10**18, 0)
        lt.set_rate(0)

    dumper = accounts[3]
    collateral_token._mint_for_testing(dumper, 5000 * 10**18)
    with boa.env.prank(dumper):
        collateral_token.approve(pool.address, 2**256 - 1)

    # NOTE: this drives only the cryptopool EMA down; price_scale stays high and the LevAMM
    # is never traded, so val marked at price_scale (v_scale) is a STALE valuation. We also
    # mark collateral at the pool's true marginal price (last_prices, which the EMA clamp
    # hides) to see real solvency.
    agg = mock_agg.price()
    rows = []
    region = []
    for step in range(40):
        gs_ok, _ = _ok(amm.get_state)
        ratio = probe.ratio_e18(pool.address)
        lp_oracle, lp_ps = probe.prices(pool.address)
        last_p = pool.last_prices()
        ps_now = pool.price_scale()
        mp = max(last_p, ps_now * 3 // 100)   # clamp above solver MIN_P (p~0.03); OVER-estimates v_true
        lp_true = probe.lp_price_at(pool.address, mp)
        coll = amm.collateral_amount()
        debt = amm.get_debt()
        v_scale = coll * lp_ps // 10**18 * agg // 10**18       # marked at stale price_scale
        v_oracle = coll * lp_oracle // 10**18 * agg // 10**18  # marked at EMA price_oracle
        v_true = coll * lp_true // 10**18 * agg // 10**18       # marked at TRUE last_prices
        po, ps = pool.price_oracle(), pool.price_scale()
        rows.append((step, ratio / 1e18, gs_ok, po, ps, last_p, v_scale, v_oracle, v_true, debt))
        if ratio < UNDERFLOW and gs_ok:
            pu_ok, pu = _ok(oracle.price_in_usd, lt.address)   # success/x0 branch (use_balances=False)
            region.append((step, v_scale / debt, v_oracle / debt, v_true / debt, pu_ok, pu))
        with boa.env.prank(dumper):
            ok, _ = _ok(pool.exchange, 1, 0, (20 + 8 * step) * 10**18, 0)
        if not ok:
            break
        boa.env.time_travel(3600)

    print("\nstep ratio gs price_oracle price_scale last_prices  v_scale/d v_oracle/d v_true/d")
    for (step, ratio, gs_ok, po, ps, last_p, v_scale, v_oracle, v_true, debt) in rows:
        print(f"{step:3d} {ratio:5.3f} {str(gs_ok)[0]}  {po/1e18:10.0f} {ps/1e18:10.0f} {last_p/1e18:10.0f}  "
              f"{v_scale/debt:8.3f} {v_oracle/debt:9.3f} {v_true/debt:8.3f}")
    print(f"\nunderflow region (ratio<9/16 AND get_state ok): {len(region)} steps")
    for (step, vs, vo, vt, pu_ok, pu) in region:
        print(f"  step {step}: v_scale/debt={vs:.3f} (stale)  v_oracle/debt={vo:.3f}  "
              f"v_true(last_prices)/debt={vt:.3f}   price_in_usd={'revert' if not pu_ok else pu}")

    # The oracle-underflow region is reached once the TRUE price (last_prices) has fallen to
    # <= price_scale/2 (the EMA clamp floor); there the position is practically insolvent at the
    # true price (v_true << debt), while get_state() still reports "OK" because it values
    # collateral at the STALE price_scale. Rather than reverting (bricking liquidations), the
    # oracle now returns 0 so a loss-taking liquidator can still clear the position.
    assert region, "never entered the (get_state ok AND oracle-underflow) region"
    for (step, vs, vo, vt, pu_ok, pu) in region:
        assert vs > 1.5         # AMM/get_state view (stale price_scale) still looks solvent
        assert vt <= vo <= vs   # true <= EMA <= stale: the staleness gap
        assert pu_ok, f"step {step}: price_in_usd reverted instead of returning 0"
        assert pu == 0, f"step {step}: price_in_usd returned {pu}, expected 0"
        assert vt < 1.5         # true-price value: position has lost ~all equity, not the ~2x the AMM sees
