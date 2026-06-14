"""
ChainSecurity #008 — YBLendingOracle must not call get_state() when use_balances is set.

With use_balances=True the x0 branch is unreachable (`success and not use_balances`), so the
get_state() raw_call (get_x0 + several AMM storage reads) is pure waste. The fix gates the
call behind `not use_balances`. This pins that the use_balances=True path is materially
cheaper -- a regression that re-introduces the call would shrink the gap.
"""
import boa


def test_use_balances_skips_get_state(factory):
    oracle = boa.load("contracts/utils/YBLendingOracle.vy")
    lt_p = boa.load_partial("contracts/LT.vy")
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

    # use_balances=True no longer pays for get_state() (+ the x0/_calculate_fresh_lv path).
    assert g_balances < g_full
    assert g_full - g_balances > 20_000   # observed ~43k on the deployed markets
