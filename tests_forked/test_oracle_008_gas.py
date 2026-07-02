"""
ChainSecurity #008 — YBLendingOracle must not do the fresh-liquidity recompute when
use_balances is set.

Originally #008 was about a wasted get_state() raw_call on the use_balances=True path;
get_state() has since been removed entirely (x0 is reproduced in-contract via _get_x0),
so neither path calls it. The gating property remains: with use_balances=True the x0
branch is unreachable (`x0_ok and not use_balances`), so the oracle skips
_calculate_fresh_lv -- the fresh LT._calculate_values replica, which makes several extra
LT staticcalls (liquidity/staker/balanceOf/totalSupply/min_admin_fee) plus the factor
math -- and takes the cheaper cached-liquidity fallback instead. This pins that the
use_balances=True path stays materially cheaper; a regression that runs the fresh-LV
recompute unconditionally would collapse the gap.

The margin here (~8k) is much smaller than the old ~43k because the big external call
(get_state re-entering the crvUSD aggregator) is gone from the baseline too.
"""


def test_use_balances_skips_fresh_lv_recompute(factory, lending_oracle, lt_deployer):
    oracle = lending_oracle
    lt_p = lt_deployer
    lt = lt_p.at(factory.markets(3).lt)

    # warm both paths, then measure
    for _ in range(2):
        oracle.price_in_usd(lt.address)
        oracle.price_in_usd(lt.address, True)
    oracle.price_in_usd(lt.address)
    g_full = oracle._computation.get_gas_used()
    oracle.price_in_usd(lt.address, True)
    g_balances = oracle._computation.get_gas_used()

    print(f"\nuse_balances=False: {g_full} gas   use_balances=True: {g_balances} gas   "
          f"saving: {g_full - g_balances}")

    # use_balances=True skips _calculate_fresh_lv (+ the factor/x0-marking work).
    assert g_balances < g_full
    assert g_full - g_balances > 4_000   # observed ~8.2k on the deployed markets
