# @version 0.4.3
"""
@title YBLendingOracleLL
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice EMA-smoothed ybLT price (USD or asset), cloned per market+denomination by
        YBLendingOracleLLFactory. The LP-oracle math is reproduced in-contract so the fee level
        (pool virtual_price) gets a flash-proof, cryptopool-style EMA while the price frame
        stays instantaneous. ema_time is factory-settable.
"""
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


event SetEmaTime:
    ema_time: uint256


PRECISION: constant(uint256) = 10**18
L: constant(uint256) = 2
# AMM.get_x0 leverage constant, identical to AMM.__init__ for leverage == L*PRECISION:
#   denominator = 2*leverage - PRECISION ; LEV_RATIO = leverage**2 * PRECISION // denominator**2
# (== 4/9 * 1e18 at L=2). Lets us reproduce get_x0 here without calling AMM.get_state().
LEV_RATIO: constant(uint256) = (L * PRECISION)**2 * PRECISION // (2 * L * PRECISION - PRECISION)**2
SQRT_MIN_UNSTAKED_FRACTION: constant(int256) = 10**14
MIN_STAKED_FOR_FEES: constant(int256) = 10**16
# Sane upper bound on the EMA time constant (~31.7 yr); guards a fat-finger set_ema_time.
MAX_EMA_TIME: constant(uint256) = 10**9


# Per-clone binding (set once by initialize()); storage, not immutable, so each EIP-1167 clone
# carries its own values rather than sharing the implementation's.
lt_token: public(LT)                # the LT (market) this clone prices
in_usd: public(bool)                # True: price in USD; False: price in the underlying asset
factory: public(address)            # the YBLendingOracleLLFactory allowed to set_ema_time
# EMA smoothing time constant (s) for the price. Half-life = ema_time * ln(2). This is the
# manipulation-resistance vs liquidation-lag dial; the FACTORY can retune it (set_ema_time).
ema_time: public(uint256)

# Honest EMA of the pool virtual_price (cryptopool/FastGauge style): each price_w() folds the
# PREVIOUSLY recorded vprice (vp_last) into the average and records the current one for next
# time, so a virtual_price flash-inflated and reverted within one block never enters the EMA.
# The fee level enters the price ONLY through virtual_price (D/supply == 2*vp*sqrt(ps)/1e18,
# an on-chain identity), so smoothing it smooths the whole price level; the price *frame*
# (price_oracle/price_scale) stays instantaneous and is itself the cryptopool's own EMA.
vp_ema: public(uint256)             # smoothed virtual_price; 0 until first price_w() seeds it
vp_last: public(uint256)            # virtual_price recorded last checkpoint (fed into next EMA)
vp_ema_ts: public(uint256)          # last EMA checkpoint timestamp


@external
def initialize(lt: LT, in_usd: bool, ema_time: uint256, factory: address):
    """
    @notice One-time bind of the clone to its LT, denomination, EMA time and factory.
    @dev vp_ema stays 0 (unseeded) until the first price_w(); until then price() prices off
         the raw virtual_price, so the LT need not already hold a position at deploy.
    @param lt The LT (market) this clone prices
    @param in_usd True to price in USD, False to price in the underlying asset (e.g. BTC)
    @param ema_time EMA smoothing time constant in seconds (0 < ema_time <= MAX_EMA_TIME)
    @param factory The YBLendingOracleLLFactory permitted to retune ema_time later
    """
    assert self.lt_token.address == empty(address), "Initialized"
    assert lt.address != empty(address) and factory != empty(address), "Zero"
    assert ema_time > 0 and ema_time <= MAX_EMA_TIME, "ema_time"
    self.lt_token = lt
    self.in_usd = in_usd
    self.ema_time = ema_time
    self.factory = factory
    # vp_ema stays 0 (unseeded) until the first price_w(); until then price() returns the raw
    # price, so the LT need not already hold a position at deploy.


@external
def set_ema_time(ema_time: uint256):
    """
    @notice Retune the EMA smoothing time constant. FACTORY only (which itself gates on the
            YB Factory admin / DAO), so the dial can be adjusted whenever necessary.
    @dev Does not re-seed: the next price_w() blends virtual_price under the new alpha.
         0 < ema_time <= MAX_EMA_TIME.
    @param ema_time New EMA time constant in seconds
    """
    assert msg.sender == self.factory, "Only factory"
    assert ema_time > 0 and ema_time <= MAX_EMA_TIME, "ema_time"
    self.ema_time = ema_time
    log SetEmaTime(ema_time=ema_time)


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
def _assert_not_reentrant(amm: LevAMM):
    """Read-only reentrancy guard: probe the AMM's @nonreentrant lock; the cryptopool reads
    self-guard (price_oracle()/price_scale() are @nonreentrant)."""
    ok: bool = raw_call(
        amm.address, method_id("check_nonreentrant()"),
        max_outsize=0, is_static_call=True, revert_on_failure=False)
    assert ok, "AMM reentrancy"


@internal
@view
def _price_with_vp(lt: LT, pool: IFXSwap, amm: LevAMM, agg_price: uint256, vprice: uint256) -> uint256:
    """
    @notice ybLT price for this clone's denomination (in_usd ? USD : asset), using the supplied
            (already-smoothed) virtual_price for the fee level.
    @dev The LP-oracle math is reproduced in-contract (as in YBNetPressure), so the fee level
         and the price frame are separable: pv_norm (from price_oracle/price_scale) is the
         manipulation-resistant price frame, and the level comes solely from `vprice`. Uses the
         identity D/totalSupply == 2*vprice*sqrt(price_scale)/1e18, so no pool D()/totalSupply()
         read is needed - the level is whatever smoothed vprice the caller passes in.
    @param vprice The (EMA-smoothed) pool virtual_price to price against.
    """
    price_oracle: uint256 = staticcall pool.price_oracle()
    price_scale: uint256 = staticcall pool.price_scale()

    # pv_norm: D=1 portfolio value at price_oracle - depends only on A and the price ratio, not
    # on the fee level (mirrors YBNetPressure._pool_metrics).
    A_raw: uint256 = LPOracle._scaled_A_raw_from_A(
        LPOracle._A_at_last_timestamp(LPOracle.IFXSwap(pool.address)))
    p: uint256 = price_oracle * PRECISION // price_scale
    x: uint256 = 0
    y: uint256 = 0
    x, y = LPOracle.lp_oracle_2._get_x_y(A_raw, p)
    pv_norm: uint256 = x + p * y // PRECISION

    # Both LP prices carry the fee level via the SAME (smoothed) vprice, so smoothing vprice
    # smooths the whole price level consistently. lp_price_oracle == pv_norm * D/supply, with
    # D/supply == lp_price_ps/1e18 by the identity above.
    lp_price_ps: uint256 = 2 * vprice * isqrt(price_scale * 10**18) // 10**18
    lp_price_oracle: uint256 = pv_norm * lp_price_ps // PRECISION

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

    # yb_oracle is now the USD price per LT token.
    yb_oracle = yb_oracle * lv_total // (convert(max(lv_admin, 0), uint256) + lv_total) * 10**18 // lt_supply
    if self.in_usd:
        return yb_oracle
    asset_price: uint256 = price_oracle * agg_price // 10**18
    return yb_oracle * 10**18 // asset_price


# Honest virtual_price EMA (cryptopool / FastGauge style). The reported value blends the
# PREVIOUSLY recorded vprice (vp_last, which survived into a later block) with the stored EMA,
# NEVER the current-block virtual_price - so a vprice flash-inflated and reverted within one
# block cannot move the price. dt is the time since the last price_w() checkpoint, so consumers
# should checkpoint regularly (a lending market calls price_w() on each borrow/liquidate). The
# price *frame* (price_oracle/price_scale) is read live but is itself the cryptopool's EMA.
@internal
@view
def _vp_ema(ema: uint256) -> uint256:
    """The smoothed virtual_price from committed state (no current-block vprice read).
    `ema` is the caller's already-read self.vp_ema (nonzero), passed in to save a re-read."""
    dt: uint256 = block.timestamp - self.vp_ema_ts
    if dt == 0:
        return ema
    alpha: uint256 = convert(math._wad_exp(-convert(dt * 10**18 // self.ema_time, int256)), uint256)
    return (self.vp_last * (10**18 - alpha) + ema * alpha) // 10**18


@external
@view
def price() -> uint256:
    """
    @notice EMA-smoothed ybLT price - USD if in_usd else the underlying asset - scaled to 1e18.
    @dev View path: reads agg.price() without checkpointing. The fee level uses the smoothed
         virtual_price from committed state (flash-proof); the price frame is read live.
    @return Smoothed price scaled to 1e18
    """
    lt: LT = self.lt_token
    pool: IFXSwap = staticcall lt.CRYPTOPOOL()
    amm: LevAMM = staticcall lt.amm()
    self._assert_not_reentrant(amm)
    # Unseeded (before the first price_w()): fall back to the raw virtual_price.
    vp_ema: uint256 = self.vp_ema
    vprice: uint256 = self._vp_ema(vp_ema) if vp_ema != 0 else staticcall pool.virtual_price()
    agg_price: uint256 = staticcall (staticcall lt.agg()).price()
    return self._price_with_vp(lt, pool, amm, agg_price, vprice)


@external
def price_w() -> uint256:
    """
    @notice Checkpoint and return the EMA-smoothed ybLT price (USD or asset per in_usd).
    @dev Advances the virtual_price EMA using the PREVIOUS checkpoint's vprice, records the
         current vprice for next time, and checkpoints the aggregator (agg.price_w()). The
         returned price uses the advanced EMA, so this call is unaffected by a same-tx vprice
         flash-manipulation. Consumers should call it regularly (see the EMA note above _vp_ema).
    @return Smoothed price scaled to 1e18
    """
    lt: LT = self.lt_token
    pool: IFXSwap = staticcall lt.CRYPTOPOOL()
    amm: LevAMM = staticcall lt.amm()
    self._assert_not_reentrant(amm)

    vprice_now: uint256 = staticcall pool.virtual_price()
    vp_ema: uint256 = self.vp_ema
    if vp_ema == 0:
        vp_ema = vprice_now                       # seed the EMA at the first checkpoint
    else:
        vp_ema = self._vp_ema(vp_ema)             # advance using the OLD vp_last (committed)
    self.vp_ema = vp_ema
    self.vp_ema_ts = block.timestamp
    self.vp_last = vprice_now                      # record current vprice for the NEXT advance

    agg_price: uint256 = extcall (staticcall lt.agg()).price_w()
    return self._price_with_vp(lt, pool, amm, agg_price, vp_ema)
