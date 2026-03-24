# @version 0.4.3

import liboracle


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

interface LevAMM:
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def get_state() -> AMMState: view


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
N_COINS: constant(uint256) = 2
POOL_A_PRECISION: constant(uint256) = 10_000
L: constant(uint256) = 2


@internal
@view
def _A_at_last_timestamp(pool: IFXSwap) -> uint256:
    # In case of stale pool price_oracle converges to last price and D is cached at last timestamp.
    #   If pool ramps A parameter the calculated invariant variables will be off,
    #   so we calculate them at one timestamp(of last interaction).
    # Replicates Twocrypto._A_gamma() but evaluates it at pool.last_timestamp().
    t: uint256 = staticcall pool.last_timestamp()
    future_t: uint256 = staticcall pool.future_A_gamma_time()
    future_A: uint256 = staticcall pool.future_A_gamma() >> 128

    if t >= future_t:
        return future_A

    initial_A: uint256 = staticcall pool.initial_A_gamma() >> 128
    initial_t: uint256 = staticcall pool.initial_A_gamma_time()

    if t <= initial_t:
        return initial_A

    # Interpolate linearly in the same way as Twocrypto._A_gamma().
    duration: uint256 = future_t - initial_t
    elapsed: uint256 = t - initial_t
    remaining: uint256 = duration - elapsed

    return unsafe_div(initial_A * remaining + future_A * elapsed, duration)


@internal
@view
def _scaled_A_raw_from_A(A_pool: uint256) -> uint256:
    # Pool stores A as: A_true * N_COINS**(N_COINS-1) * 10_000.
    # Solver expects: A_true * solver.A_PRECISION.
    return unsafe_div(
        A_pool * liboracle.A_PRECISION,
        N_COINS**(N_COINS-1) * POOL_A_PRECISION
    )


@external
@view
def price(lt: LT) -> uint256:
    pool: IFXSwap = staticcall lt.CRYPTOPOOL()
    amm: LevAMM = staticcall lt.amm()
    agg_price: uint256 = staticcall (staticcall lt.agg()).price()

    price_oracle: uint256 = staticcall pool.price_oracle()
    price_scale: uint256 = staticcall pool.price_scale()
    vprice: uint256 = staticcall pool.virtual_price()
    D: uint256 = staticcall pool.D()
    pool_supply: uint256 = staticcall pool.totalSupply()

    amm_state: AMMState = staticcall amm.get_state()
    lv: LiquidityValues = staticcall lt.liquidity()
    lt_supply: uint256 = staticcall lt.totalSupply()

    lp_price_ps: uint256 = 2 * vprice * isqrt(price_scale * 10**18) // 10**18

    # Calculating the LP oracle value
    portfolio_value: uint256 = liboracle._portfolio_value(
        self._scaled_A_raw_from_A(self._A_at_last_timestamp(pool)),
        price_oracle * PRECISION // price_scale,
    )
    lp_price_oracle: uint256 = portfolio_value * D // pool_supply

    # yb_oracle_value = x0 * (2 * L / (2*L - 1) * (lp_price_oracle / lp_price_ps)**0.5 - 1) <- agg price cancels out
    # yb_oracle_value *= f_lp / lt_supply / price_oracle
    yb_oracle: uint256 = amm_state.x0 * (
        isqrt(10**18 * lp_price_oracle // lp_price_ps) * (2 * L) // (2 * L - 1) - 10**18
    ) // 10**18
    # Make it per LT token
    yb_oracle = yb_oracle * lv.total // (convert(max(lv.admin, 0), uint256) + lv.total) * 10**18 // lt_supply
    # Make it in BTC. x0 was calculated taking crvUSD price aggregator into account -> use that
    yb_oracle = yb_oracle * 10**18 // (price_oracle * agg_price // 10**18)

    return yb_oracle
