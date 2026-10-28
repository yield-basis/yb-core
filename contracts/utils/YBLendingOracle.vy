# @version 0.4.3
"""
@title YBLendingOracle
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Stateless ybLT price oracle (USD or asset per LT); reproduces the LP-oracle + LT
        liquidity math in-contract and spawns the per-market YBPriceProxy price() clones.
"""
from ..twocrypto_lp_oracle.contracts.main import LPOracle
from snekmate.utils import math


interface Factory:
    def markets(i: uint256) -> Market: view
    def market_count() -> uint256: view

interface PriceProxy:
    def initialize(oracle: address, lt: address, in_usd: bool): nonpayable

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
    def A() -> uint256: view
    def gamma() -> uint256: view
    def precisions() -> uint256[2]: view
    def balances(i: uint256) -> uint256: view
    def MATH() -> PoolMath: view

interface PoolMath:
    def newton_D(ANN: uint256, gamma: uint256, x_unsorted: uint256[2], K0_prev: uint256) -> uint256: view

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

interface ILiquidityGauge:
    def totalSupply() -> uint256: view


struct LiquidityValues:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256

struct Market:
    asset_token: address
    cryptopool: address
    amm: address
    lt: address
    price_oracle: address
    virtual_pool: address
    staker: address

event OraclesCreated:
    market_id: indexed(uint256)
    lt: indexed(address)
    usd_oracle: address
    asset_oracle: address


PRECISION: constant(uint256) = 10**18
L: constant(uint256) = 2
# AMM.get_x0 leverage constant, identical to AMM.__init__ for leverage == L*PRECISION:
#   denominator = 2*leverage - PRECISION ; LEV_RATIO = leverage**2 * PRECISION // denominator**2
# (== 4/9 * 1e18 at L=2). Lets us reproduce get_x0 here without calling AMM.get_state().
LEV_RATIO: constant(uint256) = (L * PRECISION)**2 * PRECISION // (2 * L * PRECISION - PRECISION)**2
SQRT_MIN_UNSTAKED_FRACTION: constant(int256) = 10**14
MIN_STAKED_FOR_FEES: constant(int256) = 10**16
# sqrt(AMM price / its price oracle) where debt / collateral value reaches AMM.MAX_SAFE_DEBT (17/32)
# and AMM.MIN_SAFE_DEBT (1/16): along the AMM curve (fixed x0 and price oracle) that ratio is
# 3/2 * s - s**2 at L=2, so s = 3/4 + sqrt(9/16 - limit).
SQRT_P_MAX_DEBT: constant(uint256) = 926776695296636881     # 3/4 + sqrt(2)/8
SQRT_P_MIN_DEBT: constant(uint256) = 1457106781186547524    # 3/4 + sqrt(2)/2


FACTORY: public(immutable(Factory))
PROXY_IMPL: public(immutable(address))

# market_id -> spawned price() proxy (0 until created)
usd_oracle: public(HashMap[uint256, address])
asset_oracle: public(HashMap[uint256, address])


@deploy
def __init__(factory: Factory, proxy_impl: address):
    """
    @notice Bind the singleton to the YB factory and the price() proxy implementation.
    @param factory YB factory, used to resolve market-id -> LT in create_oracles
    @param proxy_impl YBPriceProxy implementation cloned per market by create_oracles
    """
    assert factory.address != empty(address) and proxy_impl != empty(address), "Zero"
    FACTORY = factory
    PROXY_IMPL = proxy_impl


@external
def create_oracles(market_id: uint256) -> (address, address):
    """
    @notice Spawn the USD + asset price() proxies for a factory market. Callable by anyone;
            the LT is resolved (and existence checked) via the factory market id. Idempotent:
            returns the existing pair if already created.
    @param market_id Index of the market in the YB factory
    @return (usd_oracle, asset_oracle) proxy addresses
    """
    usd: address = self.usd_oracle[market_id]
    if usd != empty(address):
        return (usd, self.asset_oracle[market_id])

    assert market_id < staticcall FACTORY.market_count(), "No market"
    lt: address = (staticcall FACTORY.markets(market_id)).lt
    assert lt != empty(address), "No market"

    usd = create_minimal_proxy_to(PROXY_IMPL)
    asset: address = create_minimal_proxy_to(PROXY_IMPL)
    extcall PriceProxy(usd).initialize(self, lt, True)
    extcall PriceProxy(asset).initialize(self, lt, False)

    self.usd_oracle[market_id] = usd
    self.asset_oracle[market_id] = asset
    log OraclesCreated(market_id=market_id, lt=lt, usd_oracle=usd, asset_oracle=asset)
    return (usd, asset)


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
def _live_curve(pool: IFXSwap, price_scale: uint256) -> (uint256, uint256, uint256):
    """
    @notice The pool's (A, virtual_price, D) at the current block.
    @dev The pool caches D and virtual_price at its last interaction, solved with the A of that
         time. While an A ramp hasn't been observed by a trade (future_A_gamma_time >
         last_timestamp: the pool's own _is_ramping(), which stays True after the ramp ends or
         is stopped until the next trade), that next trade re-solves D from the stored balances
         at the current A (Twocrypto._get_D). Do the same, so the price follows the ramp instead
         of lagging it (or pairing the current A with a stale D after stop_ramp_A_gamma). A
         trade in this block already left D at the current A. virtual_price scales with D: xcp
         is linear in D at a fixed price_scale and supply. Stored balances only change through
         pool interactions, so this adds no manipulation surface.
    @return (A, virtual_price, D)
    """
    A: uint256 = staticcall pool.A()
    vprice: uint256 = staticcall pool.virtual_price()
    D: uint256 = staticcall pool.D()
    last_ts: uint256 = staticcall pool.last_timestamp()
    if last_ts < block.timestamp and staticcall pool.future_A_gamma_time() > last_ts:
        prec: uint256[2] = staticcall pool.precisions()
        xp: uint256[2] = [staticcall pool.balances(0) * prec[0],
                          staticcall pool.balances(1) * prec[1] * price_scale // PRECISION]
        D_live: uint256 = staticcall (staticcall pool.MATH()).newton_D(A, staticcall pool.gamma(), xp, 0)
        vprice = vprice * D_live // D
        D = D_live
    return A, vprice, D


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
@pure
def _sqrt_arb_endpoint(sqrt_r: uint256, x0: uint256, debt: uint256, coll_value: uint256) -> uint256:
    """
    @notice sqrt(e): where arbitrage leaves the AMM's price, relative to its price oracle, when
            the LP market price is r times that oracle (sqrt_r = sqrt(r), 1e18).
    @dev Arbitrage moves the AMM along its curve towards r, but AMM.exchange only accepts a
         final state with debt / coll_value inside [MIN_SAFE_DEBT, MAX_SAFE_DEBT], unless the
         trade moves towards the equilibrium (coll_value == 2 * debt) without crossing it. So it
         stops at the safe limit, or doesn't move at all if the AMM is already past it.
    @param sqrt_r sqrt(LP market price / AMM price oracle), 1e18
    @param x0 The AMM's (solvable) x0 at its price oracle
    @param debt The AMM's debt
    @param coll_value The AMM's collateral value at its price oracle
    @return sqrt(e), 1e18
    """
    sqrt_e: uint256 = sqrt_r
    if coll_value > 0:
        sqrt_c: uint256 = isqrt((x0 - debt) * 10**36 // coll_value)   # the AMM's price now
        if sqrt_r < sqrt_c:     # AMM above the market: arbitrage sells it LP, adding debt
            sqrt_e = max(sqrt_r, min(sqrt_c, SQRT_P_MAX_DEBT))
        else:                   # AMM below the market: arbitrage buys LP, repaying debt
            sqrt_e = min(sqrt_r, max(sqrt_c, SQRT_P_MIN_DEBT))
    return sqrt_e


@internal
@view
def _calculate_fresh_lv(lt: LT, p_o: uint256, amm_value: uint256) -> (uint256, int256, uint256, uint256):
    """
    @notice Replicates LT._calculate_values to compute up-to-date liquidity values
    @param lt The LT contract
    @param p_o Price oracle value (price_scale * agg_price / 10**18)
    @param amm_value AMM value (x0 / (2*L - 1))
    @return (total, admin, supply_tokens, staked_tokens)
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
    staked_tokens: uint256 = convert(staked - token_reduction, uint256)

    return (total, admin, supply_tokens, staked_tokens)


@internal
@view
def _price(lt: LT, use_balances: bool) -> (uint256, uint256, uint256, bool):
    """
    @return (yb_oracle_usd, asset_price_usd, staked_tokens, fresh): fresh is True on the
            price-oracle path, where staked_tokens is the staker's fresh LT balance (0 otherwise)
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
    A: uint256 = 0
    vprice: uint256 = 0
    D: uint256 = 0
    A, vprice, D = self._live_curve(pool, price_scale)
    pool_supply: uint256 = staticcall pool.totalSupply()

    lp_price_ps: uint256 = 2 * vprice * isqrt(price_scale * 10**18) // 10**18

    # Calculating the LP oracle value
    portfolio_value: uint256 = LPOracle.lp_oracle_2._portfolio_value(
        LPOracle._scaled_A_raw_from_A(A),
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
    staked_tokens: uint256 = 0
    fresh: bool = x0_ok and not use_balances
    if fresh:
        # yb_oracle_value = x0 * (2/3 * (r / sqrt(e) + sqrt(e)) - 1) <- agg price cancels out, with
        #   r = lp_price_oracle / lp_price_ps: the LP market price over the AMM's price oracle,
        #   e: the AMM's price over its oracle once arbitraged towards r (_sqrt_arb_endpoint).
        # Normally e == r: x0 * (2 * L / (2*L - 1) * r**0.5 - 1). The AMM's safe-debt limits can
        # stop the arbitrage short, which leaves LT holders more.
        # yb_oracle_value *= f_lp / lt_supply / price_oracle
        # The factor goes <= 10**18 once the leveraged equity has been wiped out at the market
        # price; return 0 instead of underflowing so a lending integrator can still liquidate the
        # (insolvent) position rather than being bricked by a reverting price.
        sqrt_r: uint256 = isqrt(10**36 * lp_price_oracle // lp_price_ps)
        sqrt_e: uint256 = self._sqrt_arb_endpoint(sqrt_r, x0, debt, p_o_amm * collateral // PRECISION)
        factor: uint256 = L * (sqrt_r * sqrt_r // sqrt_e + sqrt_e) // (2 * L - 1)
        if factor > 10**18:
            yb_oracle = x0 * (factor - 10**18) // 10**18

        # Compute fresh liquidity values (replicates LT._calculate_values)
        p_o: uint256 = price_scale * agg_price // PRECISION
        amm_value: uint256 = x0 * PRECISION // (2 * L * PRECISION - PRECISION)
        lv_total: uint256 = 0
        lv_admin: int256 = 0
        lt_supply: uint256 = 0
        lv_total, lv_admin, lt_supply, staked_tokens = self._calculate_fresh_lv(lt, p_o, amm_value)

        # Make it per LT token
        yb_oracle = yb_oracle * lv_total // (convert(max(lv_admin, 0), uint256) + lv_total) * 10**18 // lt_supply
    else:
        # AMM untradable (get_x0 would revert, so it can't trade and there is nothing to arbitrage)
        # or use_balances requested: value from balances, what LT.emergency_withdraw pays, with the
        # cached liquidity values as balances can't change in the non-tradable state.
        # 0 for an insolvent position (collateral value below debt) instead of underflowing, for
        # the same liquidation-availability reason as above.
        coll_value: uint256 = collateral * lp_price_oracle // 10**18 * agg_price // 10**18
        if coll_value > debt:
            lv: LiquidityValues = staticcall lt.liquidity()
            yb_oracle = (coll_value - debt) * lv.total // (convert(max(lv.admin, 0), uint256) + lv.total) * 10**18 // staticcall lt.totalSupply()

    return (yb_oracle, price_oracle * agg_price // 10**18, staked_tokens, fresh)


@internal
@view
def _staked_scale(lt: LT, fresh: bool, staked_tokens: uint256) -> uint256:
    """
    @notice Gauge share -> LT: the staker's LT balance over the gauge supply (1.0 without a
            staker). The balance is the fresh one from _price on its price-oracle path (what
            LT.updated_balances() returns), else the stored one, matching the balances path's
            cached liquidity values (and LT.updated_balances() reverts while untradable).
    """
    scale: uint256 = 10**18
    staker: address = staticcall lt.staker()
    if staker != empty(address):
        staker_balance: uint256 = staked_tokens
        if not fresh:
            staker_balance = staticcall lt.balanceOf(staker)
        gauge_supply: uint256 = staticcall ILiquidityGauge(staker).totalSupply()
        scale = (staker_balance + 1) * 10**18 // (gauge_supply + 1)
    return scale


@external
@view
def price_in_asset(lt: LT, use_balances: bool = False) -> uint256:
    """
    @notice ybLT price denominated in the underlying asset (e.g. BTC), per LT token.
    @param lt The LT (market) to price
    @param use_balances Value from raw AMM balances instead of the manipulation-resistant
           price-oracle path (this path is used anyway when the position is non-tradable)
    @return Price scaled to 1e18
    """
    yb_oracle: uint256 = 0
    asset_price: uint256 = 0
    staked_tokens: uint256 = 0
    fresh: bool = False
    yb_oracle, asset_price, staked_tokens, fresh = self._price(lt, use_balances)
    return yb_oracle * 10**18 // asset_price


@external
@view
def price_in_usd(lt: LT, use_balances: bool = False) -> uint256:
    """
    @notice ybLT price in USD, per LT token.
    @param lt The LT (market) to price
    @param use_balances See price_in_asset
    @return Price scaled to 1e18
    """
    return self._price(lt, use_balances)[0]


@external
@view
def staked_price_in_asset(lt: LT, use_balances: bool = False) -> uint256:
    """
    @notice Like price_in_asset, but per staked (gauge) token: scaled by the staker's LT
            balance over the gauge supply.
    @param lt The LT (market) to price
    @param use_balances See price_in_asset
    @return Price scaled to 1e18
    """
    yb_oracle: uint256 = 0
    asset_price: uint256 = 0
    staked_tokens: uint256 = 0
    fresh: bool = False
    yb_oracle, asset_price, staked_tokens, fresh = self._price(lt, use_balances)
    return yb_oracle * self._staked_scale(lt, fresh, staked_tokens) // asset_price


@external
@view
def staked_price_in_usd(lt: LT, use_balances: bool = False) -> uint256:
    """
    @notice Like price_in_usd, but per staked (gauge) token.
    @param lt The LT (market) to price
    @param use_balances See price_in_asset
    @return Price scaled to 1e18
    """
    yb_oracle: uint256 = 0
    asset_price: uint256 = 0
    staked_tokens: uint256 = 0
    fresh: bool = False
    yb_oracle, asset_price, staked_tokens, fresh = self._price(lt, use_balances)
    return yb_oracle * self._staked_scale(lt, fresh, staked_tokens) // 10**18
