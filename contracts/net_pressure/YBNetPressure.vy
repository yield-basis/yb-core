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
    def get_state() -> AMMState: view
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view

interface LT:
    def CRYPTOPOOL() -> Pool: view
    def amm() -> LevAMM: view


struct AMMState:
    collateral: uint256
    debt: uint256
    x0: uint256

struct PoolMetrics:
    x_frac: uint256           # crvUSD value fraction of the LP at price_oracle (1e18)
    lp_price_oracle: uint256  # LP price implied by price_oracle (lp_oracle_2)
    lp_price_ps: uint256      # LP price from price_scale (== PRICE_ORACLE_CONTRACT / agg)


PRECISION: constant(uint256) = 10**18
# The whole system fixes LEVERAGE = 2 * 10**18 (AMM.__init__, Factory,
# YBLendingOracle). The closed forms below assume L = 2.
L: constant(uint256) = 2


@internal
@view
def _assert_not_reentrant(amm: LevAMM):
    # Read-only reentrancy guard, mirroring YBLendingOracle: probe the AMM's
    # @nonreentrant @view lock (reverts if held). The pool side self-guards -
    # price_oracle()/price_scale() are themselves @nonreentrant.
    ok: bool = raw_call(
        amm.address, method_id("check_nonreentrant()"),
        max_outsize=0, is_static_call=True, revert_on_failure=False)
    assert ok, "AMM reentrancy"


@internal
@view
def _pool_metrics(pool: Pool) -> PoolMetrics:
    """
    @return PoolMetrics (x_frac, lp_price_oracle, lp_price_ps), all 1e18-scaled.
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
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)
    return self._pool_metrics(pool).x_frac


@external
@view
def pool_tvl_oracle(lt: LT) -> uint256:
    """
    @notice Manipulation-resistant cryptopool TVL (crvUSD), valued at price_oracle.
    @dev = lp_price_oracle * totalSupply. Used as the normalization base (half via
         /2) for the net-pressure controller; spot balances would be manipulable.
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)
    return self._pool_metrics(pool).lp_price_oracle * (staticcall pool.totalSupply()) // PRECISION


@external
@view
def net_pressure_oracle(lt: LT) -> int256:
    """
    @notice Manipulation-resistant net pressure (debt - crvUSD in LP).
    @dev When get_state()/get_x0 revert (AMM too imbalanced to be tradable) the
         bonding-curve slide is unavailable, so fall back to the AMM's raw
         collateral/debt. The Curve pool never reverts, so the crvUSD split stays
         oracle-based (non-manipulable) in both branches.
    @param lt The YB LT (market) contract.
    @return Net pressure; positive => crvUSD buy pressure on unwind.
    """
    amm: LevAMM = staticcall lt.amm()
    pool: Pool = staticcall lt.CRYPTOPOOL()
    self._assert_not_reentrant(amm)

    p_o_amm: uint256 = staticcall (staticcall amm.PRICE_ORACLE_CONTRACT()).price()
    m: PoolMetrics = self._pool_metrics(pool)

    # x0 / get_x0 revert when the AMM is too imbalanced for the leverage math.
    gas_before: uint256 = msg.gas
    success: bool = False
    response: Bytes[96] = empty(Bytes[96])
    success, response = raw_call(
        amm.address, method_id("get_state()"),
        max_outsize=96, revert_on_failure=False, is_static_call=True)

    calc_coll_value: uint256 = 0  # AMM collateral marked at the price_oracle price
    calc_debt: uint256 = 0
    if success:
        state: AMMState = abi_decode(response, AMMState)
        # coll_value_ps == p_o_amm * collateral is exactly the coll_value get_x0
        # used for x0; coll_value_true marks it at lp_price_oracle. Then slide
        # debt/collateral along the bonding curve to that price:
        # sqrt(k * m*) = sqrt(x_initial * coll_value_true), with k = x_initial *
        # collateral conserved, so this rides on the oracle price not the spot split.
        coll_value_ps: uint256 = state.collateral * p_o_amm // PRECISION
        coll_value_true: uint256 = coll_value_ps * m.lp_price_oracle // m.lp_price_ps
        calc_coll_value = isqrt((state.x0 - state.debt) * coll_value_true)
        calc_debt = state.x0 - calc_coll_value
    else:
        # AMM non-tradable: distinguish a genuine revert from a 63/64-rule gas
        # starvation (mirrors YBLendingOracle), then use the AMM's raw amounts -
        # they can't change in this state. The split stays oracle-based.
        assert msg.gas > gas_before // 16, "GAS"
        coll_value_ps: uint256 = (staticcall amm.collateral_amount()) * p_o_amm // PRECISION
        calc_coll_value = coll_value_ps * m.lp_price_oracle // m.lp_price_ps
        calc_debt = staticcall amm.get_debt()

    # net = calc_debt - crvUSD_in_LP;  crvUSD_in_LP = calc_coll_value * x_frac
    crvusd_in_lp: uint256 = calc_coll_value * m.x_frac // PRECISION
    return convert(calc_debt, int256) - convert(crvusd_in_lp, int256)


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
