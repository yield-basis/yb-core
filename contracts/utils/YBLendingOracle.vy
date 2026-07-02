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
    def updated_balances() -> (uint256, uint256): view

interface LevAMM:
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view

interface ILiquidityGauge:
    def totalSupply() -> uint256: view


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
    """
    @notice Replicates LT._calculate_values to compute up-to-date liquidity values
    @param lt The LT contract
    @param p_o Price oracle value (price_scale * agg_price / 10**18)
    @param amm_value AMM value (x0 / (2*L - 1))
    @return (total, admin, supply_tokens)
    """
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

    token_reduction: int256 = new_total_value_36 - new_staked_value_36  # Denominator
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
def _price(lt: LT, use_balances: bool) -> (uint256, uint256):
    """
    @return (yb_oracle_usd, asset_price_usd)
    """
    pool: IFXSwap = staticcall lt.CRYPTOPOOL()
    amm: LevAMM = staticcall lt.amm()
    agg_price: uint256 = staticcall (staticcall lt.agg()).price()

    # Read-only reentrancy guard: revert if we are being read mid-operation. The AMM's
    # reads (get_state/collateral_amount/get_debt) are plain views, so probe its lock
    # explicitly via the @nonreentrant @view check (reverts if the lock is held). The pool
    # side self-guards: price_oracle()/price_scale() below are themselves @nonreentrant and
    # revert if the cryptopool lock is held.
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

    # Calculating the LP oracle value
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

    if x0_ok and (not use_balances):
        # yb_oracle_value = x0 * (2 * L / (2*L - 1) * (lp_price_oracle / lp_price_ps)**0.5 - 1) <- agg price cancels out
        # yb_oracle_value *= f_lp / lt_supply / price_oracle
        # The factor goes <= 10**18 once the leveraged equity has been wiped out (ratio < 9/16);
        # return 0 instead of underflowing so a lending integrator can still liquidate the
        # (insolvent) position rather than being bricked by a reverting price.
        factor: uint256 = isqrt(10**36 * lp_price_oracle // lp_price_ps) * (2 * L) // (2 * L - 1)
        if factor > 10**18:
            yb_oracle = x0 * (factor - 10**18) // 10**18

        # Compute fresh liquidity values (replicates LT._calculate_values)
        p_o: uint256 = price_scale * agg_price // PRECISION
        amm_value: uint256 = x0 * PRECISION // (2 * L * PRECISION - PRECISION)
        lv_total, lv_admin, lt_supply = self._calculate_fresh_lv(lt, p_o, amm_value)
    else:
        # AMM too imbalanced for x0 (or use_balances requested). Balances can't change
        # in the non-tradable state, so compute value from balances directly.
        # Return 0 for an insolvent position (collateral value below debt) instead of
        # underflowing, for the same liquidation-availability reason as above.
        coll_value: uint256 = collateral * lp_price_oracle // 10**18 * agg_price // 10**18
        if coll_value > debt:
            yb_oracle = coll_value - debt

        # Fall back to cached liquidity values
        lv: LiquidityValues = staticcall lt.liquidity()
        lv_total = lv.total
        lv_admin = lv.admin
        lt_supply = staticcall lt.totalSupply()

    # Make it per LT token
    yb_oracle = yb_oracle * lv_total // (convert(max(lv_admin, 0), uint256) + lv_total) * 10**18 // lt_supply

    return (yb_oracle, price_oracle * agg_price // 10**18)


@internal
@view
def _staked_scale(lt: LT) -> uint256:
    staker: address = staticcall lt.staker()
    staker_balance: uint256 = (staticcall lt.updated_balances())[1]
    gauge_supply: uint256 = staticcall ILiquidityGauge(staker).totalSupply()
    return (staker_balance + 1) * 10**18 // (gauge_supply + 1)


@external
@view
def price_in_asset(lt: LT, use_balances: bool = False) -> uint256:
    yb_oracle: uint256 = 0
    asset_price: uint256 = 0
    yb_oracle, asset_price = self._price(lt, use_balances)
    return yb_oracle * 10**18 // asset_price


@external
@view
def price_in_usd(lt: LT, use_balances: bool = False) -> uint256:
    return self._price(lt, use_balances)[0]


@external
@view
def staked_price_in_asset(lt: LT, use_balances: bool = False) -> uint256:
    yb_oracle: uint256 = 0
    asset_price: uint256 = 0
    yb_oracle, asset_price = self._price(lt, use_balances)
    return yb_oracle * self._staked_scale(lt) // asset_price


@external
@view
def staked_price_in_usd(lt: LT, use_balances: bool = False) -> uint256:
    return self._price(lt, use_balances)[0] * self._staked_scale(lt) // 10**18
