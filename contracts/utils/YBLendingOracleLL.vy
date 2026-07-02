# @version 0.4.3

from ..twocrypto_lp_oracle.contracts.main import LPOracle
from snekmate.utils import math


interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view
    def AGG() -> address: view

interface IFXSwap:
    def last_timestamp() -> uint256: view
    def initial_A_gamma() -> uint256: view
    def initial_A_gamma_time() -> uint256: view
    def future_A_gamma() -> uint256: view
    def future_A_gamma_time() -> uint256: view
    def virtual_price() -> uint256: view
    def price_scale() -> uint256: view
    def price_oracle() -> uint256: view
    def D() -> uint256: view
    def totalSupply() -> uint256: view

interface LT:
    def CRYPTOPOOL() -> IFXSwap: view
    def agg() -> PriceOracle: view
    def amm() -> LevAMM: view
    def liquidity() -> LiquidityValues: view
    def totalSupply() -> uint256: view
    def staker() -> address: view
    def balanceOf(addr: address) -> uint256: view
    def min_admin_fee() -> uint256: view

interface LevAMM:
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view

struct LiquidityValues:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256


PRECISION: constant(uint256) = 10**18
L: constant(uint256) = 2
# AMM.get_x0 leverage constant, identical to AMM.__init__ for leverage == L*PRECISION:
#   denominator = 2*leverage - PRECISION ; LEV_RATIO = leverage**2 * PRECISION // denominator**2
# (== 4/9 * 1e18 at L=2). Lets us reproduce get_x0 here without calling AMM.get_state().
LEV_RATIO: constant(uint256) = (L * PRECISION)**2 * PRECISION // (2 * L * PRECISION - PRECISION)**2
SQRT_MIN_UNSTAKED_FRACTION: constant(int256) = 10**14
MIN_STAKED_FOR_FEES: constant(int256) = 10**16
# EMA smoothing time constant for price_in_asset (ybBTC/BTC). Half-life = EMA_TIME * ln(2)
# ~= 600s (10 min). This is the manipulation-resistance vs liquidation-lag dial.
EMA_TIME: constant(uint256) = 866


LT_TOKEN: public(immutable(LT))

cached_price: public(uint256)       # EMA of price_in_asset; 0 until first price_w() seeds it
cached_timestamp: public(uint256)


@deploy
def __init__(lt: LT):
    LT_TOKEN = lt
    self.cached_timestamp = block.timestamp
    # cached_price stays 0 (unseeded) until the first price_w(); the EMA returns the raw
    # price until then, so deployment does not require the LT to already hold a position.


@internal
@pure
def _mul_div_signed(x: int256, y: int256, denominator: int256) -> int256:
    if denominator == 0:
        return 0
    value: int256 = convert(
        math._mul_div(
            convert(abs(x), uint256),
            convert(abs(y), uint256),
            convert(abs(denominator), uint256),
            False),
        int256)
    if ((x < 0) != (y < 0)) != (denominator < 0):
        value = -value
    return value


@internal
@pure
def _get_x0(p_oracle: uint256, collateral: uint256, debt: uint256) -> (bool, uint256):
    """
    @notice AMM.get_x0(p_oracle, collateral, debt, safe_limits=False) reproduced in-contract:
            bit-for-bit identical, but returns (solvable, x0) rather than reverting on the
            discriminant underflow. Done here for gas and to avoid the OOG-vs-revert
            ambiguity of AMM.get_state() re-entering the crvUSD aggregator.
    @dev COLLATERAL_PRECISION == 1 (the cryptopool LP is 18-dec). solvable is False exactly
         when AMM.get_x0 would revert (position too imbalanced: ratio < 9/16 at L=2).
    @return (solvable, x0)
    """
    coll_value: uint256 = p_oracle * collateral // PRECISION
    d_sub: uint256 = 4 * coll_value * LEV_RATIO // PRECISION * debt
    if coll_value * coll_value < d_sub:
        return (False, 0)
    return (True, (coll_value + isqrt(coll_value * coll_value - d_sub)) * PRECISION // (2 * LEV_RATIO))


@internal
@view
def _calculate_fresh_lv(lt: LT, p_o: uint256, amm_value: uint256) -> (uint256, int256, uint256):
    prev: LiquidityValues = staticcall lt.liquidity()
    staker: address = staticcall lt.staker()
    staked: int256 = 0
    if staker != empty(address):
        staked = convert(staticcall lt.balanceOf(staker), int256)
    supply: int256 = convert(staticcall lt.totalSupply(), int256)

    f_a: int256 = convert(
        10**18 - (10**18 - staticcall lt.min_admin_fee()) * isqrt(convert(10**36 - staked * 10**36 // supply, uint256)) // 10**18,
        int256)

    cur_value: int256 = convert(amm_value * 10**18 // p_o, int256)
    prev_value: int256 = convert(prev.total, int256)
    value_change: int256 = cur_value - (prev_value + prev.admin)

    v_st: int256 = convert(prev.staked, int256)
    v_st_ideal: int256 = convert(prev.ideal_staked, int256)

    dv_use_36: int256 = 0
    v_st_loss: int256 = max(v_st_ideal - v_st, 0)
    if staked >= MIN_STAKED_FOR_FEES:
        if value_change > 0:
            v_loss: int256 = min(value_change, v_st_loss * supply // staked)
            dv_use_36 = v_loss * 10**18 + (value_change - v_loss) * (10**18 - f_a)
        else:
            dv_use_36 = value_change * 10**18
    else:
        dv_use_36 = value_change * (10**18 - f_a)

    admin: int256 = prev.admin + (value_change - dv_use_36 // 10**18)

    dv_s_36: int256 = self._mul_div_signed(dv_use_36, staked, supply)
    if dv_use_36 > 0:
        dv_s_36 = min(dv_s_36, v_st_loss * 10**18)

    new_total_value_36: int256 = max(prev_value * 10**18 + dv_use_36, 0)
    new_staked_value_36: int256 = max(v_st * 10**18 + dv_s_36, 0)

    token_reduction: int256 = new_total_value_36 - new_staked_value_36
    token_reduction = self._mul_div_signed(new_total_value_36, staked, token_reduction) - self._mul_div_signed(new_staked_value_36, supply, token_reduction)

    max_token_reduction: int256 = abs(value_change * supply // (prev_value + value_change + 1) * (10**18 - f_a) // SQRT_MIN_UNSTAKED_FRACTION)

    if staked > 0:
        token_reduction = min(token_reduction, staked - 1)
    if supply > 0:
        token_reduction = min(token_reduction, supply - 1)
    if token_reduction >= 0:
        token_reduction = min(token_reduction, max_token_reduction)
    else:
        token_reduction = max(token_reduction, -max_token_reduction)
    if new_total_value_36 - new_staked_value_36 < 10**4 * 10**18:
        token_reduction = max(token_reduction, 0)

    total: uint256 = convert(new_total_value_36 // 10**18, uint256)
    supply_tokens: uint256 = convert(supply - token_reduction, uint256)

    return (total, admin, supply_tokens)


@internal
@view
def _raw_price_in_asset(agg_price: uint256) -> uint256:
    """
    @notice Uncapped price_in_asset (ybBTC/BTC), computed like YBLendingOracle (real
            pool.virtual_price() and D/totalSupply). The EMA is applied on top of this.
    @dev agg_price is supplied by the caller so the view path can read agg.price() while
         the state-changing path can checkpoint it via agg.price_w().
    """
    lt: LT = LT_TOKEN
    pool: IFXSwap = staticcall lt.CRYPTOPOOL()
    amm: LevAMM = staticcall lt.amm()

    # Read-only reentrancy guard: probe the AMM's @nonreentrant lock; the cryptopool reads
    # below self-guard (price_oracle()/price_scale() are @nonreentrant).
    reentrancy_ok: bool = raw_call(
        amm.address, method_id("check_nonreentrant()"),
        max_outsize=0, is_static_call=True, revert_on_failure=False)
    assert reentrancy_ok, "AMM reentrancy"

    price_oracle: uint256 = staticcall pool.price_oracle()
    price_scale: uint256 = staticcall pool.price_scale()
    vprice: uint256 = staticcall pool.virtual_price()
    D: uint256 = staticcall pool.D()
    pool_supply: uint256 = staticcall pool.totalSupply()

    lp_price_ps: uint256 = 2 * vprice * isqrt(price_scale * 10**18) // 10**18

    portfolio_value: uint256 = LPOracle.lp_oracle_2._portfolio_value(
        LPOracle._scaled_A_raw_from_A(LPOracle._A_at_last_timestamp(LPOracle.IFXSwap(pool.address))),
        price_oracle * PRECISION // price_scale,
    )
    lp_price_oracle: uint256 = portfolio_value * D // pool_supply

    # x0 == AMM.get_x0(): reproduced in-contract (see _get_x0) for gas and to avoid the
    # OOG-vs-revert ambiguity of get_state() re-entering the crvUSD aggregator. p_o_amm ==
    # PRICE_ORACLE_CONTRACT.price() == lp_price_ps * agg_price / 1e18.
    collateral: uint256 = staticcall amm.collateral_amount()
    debt: uint256 = staticcall amm.get_debt()
    p_o_amm: uint256 = lp_price_ps * agg_price // PRECISION
    x0_ok: bool = False
    x0: uint256 = 0
    x0_ok, x0 = self._get_x0(p_o_amm, collateral, debt)

    yb_oracle: uint256 = 0
    lv_total: uint256 = 0
    lv_admin: int256 = 0
    lt_supply: uint256 = 0

    if x0_ok:
        # Return 0 once the leveraged equity is wiped (ratio < 9/16) instead of underflowing.
        factor: uint256 = isqrt(10**36 * lp_price_oracle // lp_price_ps) * (2 * L) // (2 * L - 1)
        if factor > 10**18:
            yb_oracle = x0 * (factor - 10**18) // 10**18
        p_o: uint256 = price_scale * agg_price // PRECISION
        amm_value: uint256 = x0 * PRECISION // (2 * L * PRECISION - PRECISION)
        lv_total, lv_admin, lt_supply = self._calculate_fresh_lv(lt, p_o, amm_value)
    else:
        # Return 0 for an insolvent position (collateral value below debt) instead of underflowing.
        coll_value: uint256 = collateral * lp_price_oracle // 10**18 * agg_price // 10**18
        if coll_value > debt:
            yb_oracle = coll_value - debt
        lv: LiquidityValues = staticcall lt.liquidity()
        lv_total = lv.total
        lv_admin = lv.admin
        lt_supply = staticcall lt.totalSupply()

    yb_oracle = yb_oracle * lv_total // (convert(max(lv_admin, 0), uint256) + lv_total) * 10**18 // lt_supply
    asset_price: uint256 = price_oracle * agg_price // 10**18

    return yb_oracle * 10**18 // asset_price


# Smoothing model & caveat (read before integrating).
# This is the standard discrete EMA (same form as Curve's price_oracle): it blends the
# *current* raw price with the stored value at weight alpha = exp(-dt/EMA_TIME), where dt is
# the time since the last price_w() checkpoint. It is symmetric (sharp moves in either
# direction decay in over ~EMA_TIME) but it is NOT a true continuous EMA -- the smoothing
# strength depends on dt, so consumers must checkpoint regularly (a lending market calls
# price_w() on each borrow/liquidate, keeping dt small). After a long idle gap (dt >>
# EMA_TIME), alpha -> 0: the FIRST interaction snaps to the current raw price (no smoothing)
# and re-anchors, and only the SECOND interaction is smoothed again.
# Idle-gap first-touch exposure differs by manipulation vector:
#   - price divergence (price_oracle/price_scale): bounded -- the raw reads the cryptopool's
#     own price_oracle EMA, so a flash spot move barely shifts it within a block;
#   - fee/wash (virtual_price): not magnitude-bounded by this EMA in the gap, but unprofitable
#     -- lifting virtual_price by a fraction costs that fraction of the WHOLE pool in fees
#     while only the LT's slice feeds an over-borrow, so it loses money unless the LT is
#     essentially the whole pool.
@internal
@view
def _ema(raw: uint256) -> uint256:
    cached: uint256 = self.cached_price
    if cached == 0:
        return raw   # unseeded: first price_w() seeds the EMA
    dt: uint256 = block.timestamp - self.cached_timestamp
    alpha: uint256 = convert(math._wad_exp(-convert(dt * 10**18 // EMA_TIME, int256)), uint256)
    return (raw * (10**18 - alpha) + cached * alpha) // 10**18


@external
@view
def price() -> uint256:
    agg_price: uint256 = staticcall (staticcall LT_TOKEN.agg()).price()
    return self._ema(self._raw_price_in_asset(agg_price))


@external
def price_w() -> uint256:
    # State-changing path: also checkpoint the aggregator (advance its EMA) via price_w().
    agg: PriceOracle = staticcall LT_TOKEN.agg()
    agg_price: uint256 = extcall agg.price_w()
    ema: uint256 = self._ema(self._raw_price_in_asset(agg_price))
    self.cached_price = ema
    self.cached_timestamp = block.timestamp
    return ema
