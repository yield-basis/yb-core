"""
Consistency of YBLendingOracle's two valuations, as a function of ratio = price_oracle/price_scale.

  price_in_usd(use_balances=False) -> success/x0 branch  = equity*(4*sqrt(ratio) - 3)
  price_in_usd(use_balances=True)  -> balances branch    = equity*(2*ratio - 1)  ("portfolio value - debt")

Analytically the relative gap is 2*(sqrt(ratio)-1)**2 / (2*ratio-1), which is 0 at ratio=1
(price_oracle == price_scale) and grows as the EMA departs from price_scale. So once
price_oracle has settled near price_scale, price_in_usd ~= portfolio value - debt.
"""
import boa


def _ok(fn, *a):
    try:
        return True, fn(*a)
    except Exception as e:
        return False, str(e)


def test_price_in_usd_matches_portfolio_minus_debt(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, lending_oracle, ratio_probe,
):
    oracle = lending_oracle
    probe = ratio_probe

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

    print("\n ratio   price_in_usd(x0)   portfolio-debt   rel_gap   predicted 2(√r-1)^2/(2r-1)")
    checked = 0
    for step in range(14):
        # Let price_oracle (EMA) settle to the current last_prices: time-travel, no trades.
        for _ in range(8):
            boa.env.time_travel(1200)
        ratio = probe.ratio_e18(cryptopool.address) / 1e18

        gs_ok, _ = _ok(yb_amm.get_state)
        a_ok, A = _ok(oracle.price_in_usd, yb_lt.address)          # success/x0 branch
        b_ok, B = _ok(oracle.price_in_usd, yb_lt.address, True)    # balances branch

        if gs_ok and a_ok and b_ok and A > 0:
            rel_gap = (B - A) / B
            r = ratio ** 0.5
            predicted = 2 * (r - 1) ** 2 / (2 * ratio - 1) if (2 * ratio - 1) > 0 else float("nan")
            print(f" {ratio:5.3f}   {A/1e18:14.6f}   {B/1e18:13.6f}   {rel_gap:7.4f}   {predicted:7.4f}")
            checked += 1
            # The two valuations agree to within the analytic (sqrt(ratio)-1)^2 law.
            assert abs(rel_gap - predicted) < 0.01, (
                f"ratio={ratio}: rel_gap {rel_gap:.4f} vs predicted {predicted:.4f}")
            if ratio > 0.99:
                # price_oracle settled at price_scale -> price_in_usd ~= portfolio value - debt.
                assert rel_gap < 1e-3, f"at ratio~1 the two should match: rel_gap={rel_gap}"

        # push the price down a bit for the next ratio sample
        with boa.env.prank(dumper):
            ok, _ = _ok(cryptopool.exchange, 1, 0, 2 * 10**18, 0)
        if not ok:
            break

    assert checked >= 4
