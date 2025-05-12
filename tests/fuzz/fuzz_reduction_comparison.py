from hypothesis import given, settings
from hypothesis import strategies as st


SQRT_MIN_UNSTAKED_FRACTION = 10**14
USE_LIMIT = True


@given(
    prev_value=st.integers(min_value=10**18, max_value=10**20),
    # staked_frac=st.floats(min_value=0.99, max_value=0.9999),
    supply=st.integers(min_value=10**17, max_value=10**20),
    staked_diff=st.integers(min_value=1, max_value=10**5),
    value_change=st.integers(min_value=-10**18, max_value=-10**17),
)
@settings(max_examples=5000)
def test_token_reduction_precision(prev_value, staked_diff, supply, value_change):
    from math import isqrt
    _min_admin_fee = 10**17

    staked = supply - staked_diff

    v_st = prev_value * staked // supply
    v_st_ideal = v_st

    f_a = 10**18 - (10**18 - _min_admin_fee) * isqrt(10**36 - staked * 10**36 // supply) // 10**18

    dv_use = value_change * (10**18 - f_a) // 10**18
    dv_s = dv_use * staked // supply
    if dv_use > 0:
        dv_s = min(dv_s, max(v_st_ideal - v_st, 0))
    new_total_value = max(prev_value + dv_use, 0)
    new_staked_value = max(v_st + dv_s, 0)

    token_reduction = (staked * new_total_value - new_staked_value * supply) // (new_total_value - new_staked_value)
    max_token_reduction = abs(value_change * supply // (prev_value + value_change + 1) * (10**18 - f_a) // SQRT_MIN_UNSTAKED_FRACTION)

    if USE_LIMIT:
        # let's leave at least 1 LP token for staked and for total
        if staked > 0:
            token_reduction = min(token_reduction, staked - 1)
        if supply > 0:
            token_reduction = min(token_reduction, supply - 1)
        # But most likely it's this condition to apply
        if token_reduction >= 0:
            token_reduction = min(token_reduction, max_token_reduction)
        else:
            token_reduction = max(token_reduction, -max_token_reduction)
        # And don't allow negatives if denominator was too small
        if new_total_value - new_staked_value < 10**4:
            token_reduction = max(token_reduction, 0)

    # no // 10**18
    dv_use_ = value_change * (10**18 - f_a)
    dv_s_ = dv_use_ * staked // supply
    if dv_use_ > 0:
        dv_s_ = min(dv_s_, 10**18 * max(v_st_ideal - v_st, 0))

    new_total_value_ = max(prev_value * 10**18 + dv_use_, 0)
    new_staked_value_ = max(v_st * 10**18 + dv_s_, 0)

    token_reduction_precise = (staked * new_total_value_ - new_staked_value_ * supply) // (new_total_value_ - new_staked_value_)

    if USE_LIMIT:
        # let's leave at least 1 LP token for staked and for total
        if staked > 0:
            token_reduction_precise = min(token_reduction_precise, staked - 1)
        if supply > 0:
            token_reduction_precise = min(token_reduction_precise, supply - 1)
        # But most likely it's this condition to apply
        if token_reduction_precise >= 0:
            token_reduction_precise = min(token_reduction_precise, max_token_reduction)
        else:
            token_reduction_precise = max(token_reduction_precise, -max_token_reduction)
        # And don't allow negatives if denominator was too small
        if new_total_value - new_staked_value < 10**4 * 10**18:
            token_reduction_precise = max(token_reduction_precise, 0)

    assert token_reduction_precise >= 0
    assert abs(token_reduction - token_reduction_precise) <= 10**12
    assert abs(token_reduction) >= token_reduction_precise
