# @version 0.4.3
"""
@title YBNetPressure
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Computes the crvUSD "net pressure" of a YB market: the AMM's debt minus
        the amount of crvUSD sitting inside the Curve LP tokens the YB AMM holds.

        Positive net pressure => debt exceeds the crvUSD that unwinding the LP
        would free, i.e. crvUSD has to be *bought* to repay (buy pressure).
        Negative => the LP holds more crvUSD than the debt (sell pressure).

        Two flavours:
          * net_pressure_naive  - spot subtraction. Cheap, but MANIPULABLE: the
            pool's crvUSD reserve and the AMM collateral/debt are spot quantities
            a swap can move within the block.
          * net_pressure_oracle - manipulation-resistant. Built from the AMM's
            conserved invariants (x0 and the constant product) and the pool
            composition implied by the pool's price_oracle (EMA) via curve-std's
            lp_oracle_2 solver - the same pieces used by YBLendingOracle/LPOracle.

@dev Derivation of the oracle flavour (leverage L fixed at 2 system-wide).

     The LevAMM is a constant product with virtual stablecoin reserve
     x = x0 - debt and collateral y:
         x * y = k         (conserved by trades, AMM.exchange)
         x0                (conserved by trades, AMM.get_x0 check)
     so k and x0 are the manipulation-resistant anchors; debt and collateral are
     just where the curve currently sits. The AMM marginal price is the
     bonding-curve derivative
         m = d(debt)/d(collateral) = x/y = (x0 - debt)/collateral   (AMM.get_p)

     The position sits at the price its own PRICE_ORACLE_CONTRACT reports
     (lp_price_ps, from price_scale), which differs from the LP price LPOracle
     gives at price_oracle (lp_price_oracle). We slide debt along the curve to
     that target price m* = lp_price_oracle:
         x(m) = sqrt(k*m)  =>  calculated_debt = x0 - sqrt(k * m*)
     Equivalently, since x*collateral = k is conserved,
         sqrt(k * m*) = sqrt((x0 - debt) * coll_value_true)
     where coll_value_true is the value of the *current* collateral at m*. At the
     marked point the collateral value equals x again (coll_value = m*sqrt(k/m) =
     sqrt(k*m)), so:
         calculated_coll_value = sqrt(k * m*) = x0 - calculated_debt
         crvUSD_in_LP          = calculated_coll_value * x_frac
         net = calculated_debt - crvUSD_in_LP = x0 - sqrt(k * m*) * (1 + x_frac)
     with x_frac the crvUSD value fraction of the LP at price_oracle (lp_oracle_2).

     Cross-check: calculated_coll_value - calculated_debt == YBLendingOracle's
     equity x0*(2L/(2L-1)*sqrt(r) - 1), r = lp_price_oracle/lp_price_ps. At r == 1
     calculated_debt == debt and net == 0 (a 2x 50/50 LP has debt == crvUSD-in-LP).

     Numeraire: x0 and PRICE_ORACLE_CONTRACT carry the agg (USD) factor and the
     AMM treats 1 crvUSD as 1 USD-unit in its leverage math, so net pressure is
     reported in that numeraire (== crvUSD when crvUSD is at peg). The lp_price_ps
     factor cancels between coll_value (via PRICE_ORACLE_CONTRACT) and the ratio,
     so the result rides on lp_price_oracle and the conserved k/x0.
"""
from ..twocrypto_lp_oracle.contracts.main import LPOracle


interface PriceOracle:
    def price() -> uint256: view

interface Pool:
    def price_oracle() -> uint256: view
    def price_scale() -> uint256: view
    def virtual_price() -> uint256: view
    def D() -> uint256: view
    def balances(i: uint256) -> uint256: view
    def totalSupply() -> uint256: view

interface LevAMM:
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view

interface LT:
    def CRYPTOPOOL() -> Pool: view
    def amm() -> LevAMM: view


struct PoolMetrics:
    x_frac: uint256           # crvUSD value fraction of the LP at price_oracle (1e18)
    lp_price_oracle: uint256  # LP price implied by price_oracle (lp_oracle_2)
    lp_price_ps: uint256      # LP price from price_scale (== PRICE_ORACLE_CONTRACT / agg)

struct PressureTvl:
    net_pressure: int256      # debt - crvUSD in LP (crvUSD); positive => buy pressure
    half_tvl: uint256         # AMM equity at price_oracle (crvUSD); the normalizer


PRECISION: constant(uint256) = 10**18
# The whole system fixes LEVERAGE = 2 * 10**18 (AMM.__init__, Factory,
# YBLendingOracle). The closed forms below assume L = 2.
L: constant(uint256) = 2
# AMM.get_x0 leverage constant, identical to AMM.__init__ for leverage == L*PRECISION:
#   denominator = 2*leverage - PRECISION ; LEV_RATIO = leverage**2 * PRECISION // denominator**2
# (== 4/9 * 1e18 at L=2). Lets us reproduce get_x0 here without calling AMM.get_state().
LEV_RATIO: constant(uint256) = (L * PRECISION)**2 * PRECISION // (2 * L * PRECISION - PRECISION)**2


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
def _assert_not_reentrant(amm: LevAMM):
    """
    @notice Read-only reentrancy guard: revert if the AMM lock is held.
    @dev Mirrors YBLendingOracle: probe the AMM's @nonreentrant @view lock. The pool
         side self-guards - price_oracle()/price_scale() are themselves @nonreentrant.
    @param amm The market's LevAMM whose lock is probed.
    """
    ok: bool = raw_call(
        amm.address, method_id("check_nonreentrant()"),
        max_outsize=0, is_static_call=True, revert_on_failure=False)
    assert ok, "AMM reentrancy"


@internal
@view
def _pool_metrics(pool: Pool) -> PoolMetrics:
    """
    @notice Oracle-priced metrics for the cryptopool, all 1e18-scaled.
    @param pool The Curve twocrypto pool (crvUSD/asset).
    @return PoolMetrics(x_frac, lp_price_oracle, lp_price_ps): crvUSD value fraction
            of the LP at price_oracle, the LP price implied by price_oracle, and the
            LP price from price_scale (== PRICE_ORACLE_CONTRACT / agg).
    """
    price_oracle: uint256 = staticcall pool.price_oracle()
    price_scale: uint256 = staticcall pool.price_scale()
    vprice: uint256 = staticcall pool.virtual_price()
    D: uint256 = staticcall pool.D()
    pool_supply: uint256 = staticcall pool.totalSupply()

    A_raw: uint256 = LPOracle._scaled_A_raw_from_A(
        LPOracle._A_at_last_timestamp(LPOracle.IFXSwap(pool.address)))
    p: uint256 = price_oracle * PRECISION // price_scale

    x: uint256 = 0
    y: uint256 = 0
    x, y = LPOracle.lp_oracle_2._get_x_y(A_raw, p)
    pv_norm: uint256 = x + p * y // PRECISION                 # D=1 portfolio value
    return PoolMetrics(
        x_frac=x * PRECISION // pv_norm,
        lp_price_oracle=pv_norm * D // pool_supply,
        lp_price_ps=2 * vprice * isqrt(price_scale * PRECISION) // PRECISION,
    )


@external
@view
def crvusd_value_fraction(lt: LT) -> uint256:
    """
    @notice crvUSD value fraction (1e18) of the LP at the pool's price_oracle.
    @dev Component of net_pressure_oracle, exposed for monitoring/testing.
    @param lt The YB LT (market) contract.
    @return crvUSD value fraction of the LP, 1e18 == 100%.
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)
    return self._pool_metrics(pool).x_frac


@internal
@view
def _pressure_signals(amm: LevAMM, m: PoolMetrics) -> PressureTvl:
    """
    @notice Net pressure AND half-TVL for the AMM, both manipulation-resistant.
    @dev Both are derived from the AMM's conserved invariants (x0 and the constant
         product k), marked at price_oracle on the bonding curve - NOT from the spot
         collateral_amount, which a crvUSD<->LP trade against the AMM could inflate.
           calc_coll_value = sqrt(k * lp_price_oracle)   (k = x_initial*collateral)
           calc_debt       = x0 - calc_coll_value
           net_pressure    = calc_debt - calc_coll_value * x_frac
           half_tvl        = calc_coll_value - calc_debt  (AMM equity at price_oracle;
                             == value_oracle at equilibrium)
         When get_state()/get_x0 revert (AMM untradable) we fall back to the AMM's raw
         collateral/debt - safe there because nothing can trade against it.
    @param amm The market's LevAMM.
    @param m The pool's already-computed metrics.
    @return PressureTvl(net_pressure, half_tvl), crvUSD numeraire.
    """
    p_o_amm: uint256 = staticcall (staticcall amm.PRICE_ORACLE_CONTRACT()).price()
    collateral: uint256 = staticcall amm.collateral_amount()
    debt: uint256 = staticcall amm.get_debt()

    # x0 == AMM.get_x0(): reproduced in-contract (see _get_x0) for gas and to avoid the
    # OOG-vs-revert ambiguity of get_state() re-entering the crvUSD aggregator.
    x0_ok: bool = False
    x0: uint256 = 0
    x0_ok, x0 = self._get_x0(p_o_amm, collateral, debt)
    # get_x0's coll_value: the collateral marked at the price_scale price (p_o_amm).
    coll_value_ps: uint256 = p_o_amm * collateral // PRECISION

    calc_coll_value: uint256 = 0  # AMM collateral marked at the price_oracle price
    calc_debt: uint256 = 0
    if x0_ok:
        # Solvable: x0 == get_x0(...). coll_value_true marks the collateral at
        # lp_price_oracle, then slide debt/collateral along the bonding curve to that
        # price: sqrt(k * m*) = sqrt(x_initial * coll_value_true), with k = x_initial
        # * collateral conserved, so this rides on the oracle price not the spot split.
        coll_value_true: uint256 = coll_value_ps * m.lp_price_oracle // m.lp_price_ps
        calc_coll_value = isqrt((x0 - debt) * coll_value_true)
        calc_debt = x0 - calc_coll_value
    else:
        # AMM non-tradable (get_x0 would revert): use the AMM's raw amounts - they
        # can't change in this state. The split stays oracle-based.
        calc_coll_value = coll_value_ps * m.lp_price_oracle // m.lp_price_ps
        calc_debt = debt

    # net = calc_debt - crvUSD_in_LP;  crvUSD_in_LP = calc_coll_value * x_frac
    crvusd_in_lp: uint256 = calc_coll_value * m.x_frac // PRECISION
    # half-TVL = AMM equity at price_oracle; 0 if the position is underwater.
    half_tvl: uint256 = 0
    if calc_coll_value > calc_debt:
        half_tvl = calc_coll_value - calc_debt
    return PressureTvl(
        net_pressure=convert(calc_debt, int256) - convert(crvusd_in_lp, int256),
        half_tvl=half_tvl,
    )


@external
@view
def half_tvl_oracle(lt: LT) -> uint256:
    """
    @notice The AMM's half-TVL: its equity valued at price_oracle (crvUSD).
    @dev Derived from the conserved x0/constant product (== value_oracle at
         equilibrium), not the spot collateral_amount, so it is non-manipulable.
         The controller's normalizer.
    @param lt The YB LT (market) contract.
    @return AMM equity at price_oracle in crvUSD (1e18).
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)
    return self._pressure_signals(amm, self._pool_metrics(pool)).half_tvl


@external
@view
def net_pressure_oracle(lt: LT) -> int256:
    """
    @notice Manipulation-resistant net pressure (debt - crvUSD in LP).
    @param lt The YB LT (market) contract.
    @return Net pressure; positive => crvUSD buy pressure on unwind.
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)
    return self._pressure_signals(amm, self._pool_metrics(pool)).net_pressure


@external
@view
def net_pressure_and_tvl(lt: LT) -> PressureTvl:
    """
    @notice Net pressure and the AMM's half-TVL in a single call.
    @dev Computes the (expensive) lp_oracle_2 pool metrics once and reuses them for
         both, so the controller's per-pool aggregation doesn't pay for it twice.
    @param lt The YB LT (market) contract.
    @return PressureTvl(net_pressure, half_tvl); both manipulation-resistant, crvUSD.
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)
    return self._pressure_signals(amm, self._pool_metrics(pool))


@external
@view
def net_pressure_naive(lt: LT) -> int256:
    """
    @notice Spot net pressure (debt - crvUSD in LP). MANIPULABLE.
    @dev Uses the pool's spot crvUSD reserve and the AMM's spot collateral/debt,
         all of which a swap can move within the block. For monitoring only.
    @param lt The YB LT (market) contract.
    @return Net pressure; positive => crvUSD buy pressure on unwind.
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)

    debt: uint256 = staticcall amm.get_debt()
    collateral: uint256 = staticcall amm.collateral_amount()  # LP held by the AMM
    crvusd_reserve: uint256 = staticcall pool.balances(0)     # coin0 = crvUSD
    pool_supply: uint256 = staticcall pool.totalSupply()

    # crvUSD inside the LP tokens the AMM holds.
    crvusd_in_lp: uint256 = collateral * crvusd_reserve // pool_supply

    return convert(debt, int256) - convert(crvusd_in_lp, int256)
