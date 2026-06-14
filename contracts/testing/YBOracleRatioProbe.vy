# @version 0.4.3
# Test-only probe: returns the exact ratio that drives the underflow in
# YBLendingOracle._price's success branch, namely
#   lp_price_oracle / lp_price_ps   (scaled by 1e18).
# The oracle computes isqrt(10**36 * lp_price_oracle // lp_price_ps) * (2L)//(2L-1) - 10**18,
# which underflows (reverts) exactly when this ratio < (3/4)^2 = 9/16 = 0.5625e18.
# Mirrors contracts/utils/YBLendingOracle.vy lines computing lp_price_ps / lp_price_oracle.
from ..twocrypto_lp_oracle.contracts.main import LPOracle

PRECISION: constant(uint256) = 10**18
UNDERFLOW_THRESHOLD: public(constant(uint256)) = 9 * 10**18 // 16  # 0.5625e18


@internal
@view
def _prices(pool: address) -> (uint256, uint256):
    # returns (lp_price_oracle, lp_price_ps), exactly as YBLendingOracle computes them
    p: LPOracle.IFXSwap = LPOracle.IFXSwap(pool)
    price_oracle: uint256 = staticcall p.price_oracle()
    price_scale: uint256 = staticcall p.price_scale()
    vprice: uint256 = staticcall p.virtual_price()
    D: uint256 = staticcall p.D()
    pool_supply: uint256 = staticcall p.totalSupply()

    lp_price_ps: uint256 = 2 * vprice * isqrt(price_scale * 10**18) // 10**18
    pv: uint256 = LPOracle.lp_oracle_2._portfolio_value(
        LPOracle._scaled_A_raw_from_A(LPOracle._A_at_last_timestamp(p)),
        price_oracle * PRECISION // price_scale,
    )
    lp_price_oracle: uint256 = pv * D // pool_supply
    return (lp_price_oracle, lp_price_ps)


@external
@view
def ratio_e18(pool: address) -> uint256:
    lp_price_oracle: uint256 = 0
    lp_price_ps: uint256 = 0
    lp_price_oracle, lp_price_ps = self._prices(pool)
    return lp_price_oracle * PRECISION // lp_price_ps


@external
@view
def prices(pool: address) -> (uint256, uint256):
    # (lp_price_oracle, lp_price_ps): EMA/portfolio-value LP price and spot/scale LP price
    return self._prices(pool)


@external
@view
def lp_price_at(pool: address, market_price: uint256) -> uint256:
    # LP price per token implied by an arbitrary marginal `market_price`
    # (portfolio_value at p = market_price / price_scale). Lets us mark collateral at the
    # pool's true last_prices instead of the stale price_scale.
    p: LPOracle.IFXSwap = LPOracle.IFXSwap(pool)
    price_scale: uint256 = staticcall p.price_scale()
    D: uint256 = staticcall p.D()
    pool_supply: uint256 = staticcall p.totalSupply()
    pv: uint256 = LPOracle.lp_oracle_2._portfolio_value(
        LPOracle._scaled_A_raw_from_A(LPOracle._A_at_last_timestamp(p)),
        market_price * PRECISION // price_scale,
    )
    return pv * D // pool_supply
