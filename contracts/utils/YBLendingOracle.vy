# @version 0.4.3

from ..twocrypto_ng.contracts.main import LPOracle
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
    def get_state() -> AMMState: view
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view


struct AMMState:
    collateral: uint256
    debt: uint256
    x0: uint256

struct LiquidityValues:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256


PRECISION: constant(uint256) = 10**18
L: constant(uint256) = 2
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

    # Try to get AMM state (may revert if AMM is too imbalanced for x0 calculation)
    yb_oracle: uint256 = 0
    success: bool = False
    response: Bytes[96] = empty(Bytes[96])
    success, response = raw_call(
        amm.address,
        method_id("get_state()"),
        max_outsize=96,
        revert_on_failure=False,
        is_static_call=True
    )

    lv_total: uint256 = 0
    lv_admin: int256 = 0
    lt_supply: uint256 = 0

    if success and not use_balances:
        # yb_oracle_value = x0 * (2 * L / (2*L - 1) * (lp_price_oracle / lp_price_ps)**0.5 - 1) <- agg price cancels out
        # yb_oracle_value *= f_lp / lt_supply / price_oracle
        amm_state: AMMState = abi_decode(response, AMMState)
        yb_oracle = amm_state.x0 * (
            isqrt(10**36 * lp_price_oracle // lp_price_ps) * (2 * L) // (2 * L - 1) - 10**18
        ) // 10**18

        # Compute fresh liquidity values (replicates LT._calculate_values)
        p_o: uint256 = price_scale * agg_price // PRECISION
        amm_value: uint256 = amm_state.x0 * PRECISION // (2 * L * PRECISION - PRECISION)
        lv_total, lv_admin, lt_supply = self._calculate_fresh_lv(lt, p_o, amm_value)
    else:
        # AMM is too imbalanced for x0 to be calculated.
        # Balances can't change in this state, so compute value from balances directly.
        collateral: uint256 = staticcall amm.collateral_amount()
        debt: uint256 = staticcall amm.get_debt()
        yb_oracle = collateral * lp_price_oracle // 10**18 * agg_price // 10**18 - debt

        # Fall back to cached liquidity values
        lv: LiquidityValues = staticcall lt.liquidity()
        lv_total = lv.total
        lv_admin = lv.admin
        lt_supply = staticcall lt.totalSupply()

    # Make it per LT token
    yb_oracle = yb_oracle * lv_total // (convert(max(lv_admin, 0), uint256) + lv_total) * 10**18 // lt_supply

    return (yb_oracle, price_oracle * agg_price // 10**18)


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
