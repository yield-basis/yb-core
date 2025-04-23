# pragma version ~=0.4.1
"""
@title TwocryptoView
@author Curve.Fi
@license Copyright (c) Curve.Fi, 2025 - all rights reserved
@notice This contract contains view-only external methods which can be
        gas-inefficient when called from smart contracts.
"""

from ethereum.ercs import IERC20


interface Curve:
    # def MATH() -> Math: view
    # def A() -> uint256: view
    # def gamma() -> uint256: view
    # def price_scale() -> uint256: view
    # def price_oracle() -> uint256: view
    # def get_virtual_price() -> uint256: view
    # def balances(i: uint256) -> uint256: view
    # def D() -> uint256: view
    # def fee_calc(xp: uint256[N_COINS]) -> uint256: view
    # def calc_token_fee(
    #     amounts: uint256[N_COINS], xp: uint256[N_COINS]
    # ) -> uint256: view
    # def future_A_gamma_time() -> uint256: view
    # def totalSupply() -> uint256: view
    # def precisions() -> uint256[N_COINS]: view
    # def packed_fee_params() -> uint256: view
    def calc_withdraw_fixed_out(lp_token_amount: uint256, i: uint256, amount_i: uint256) -> uint256: view
    def calc_withdraw_one_coin(lp_token_amount: uint256, i: uint256) -> uint256: view
    def fee_receiver() -> address: view
    def admin() -> address: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view
    def get_dy(i: uint256, j: uint256, dx: uint256) -> uint256: view
    def get_dx(i: uint256, j: uint256, dy: uint256) -> uint256: view
    def lp_price() -> uint256: view
    def get_virtual_price() -> uint256: view
    def price_oracle() -> uint256: view
    def price_scale() -> uint256: view
    def fee() -> uint256: view
    def calc_token_fee(amounts: uint256[2], xp: uint256[2]) -> uint256: view
    def A() -> uint256: view
    def gamma() -> uint256: view
    def mid_fee() -> uint256: view
    def out_fee() -> uint256: view
    def fee_gamma() -> uint256: view
    def allowed_extra_profit() -> uint256: view
    def adjustment_step() -> uint256: view
    def ma_time() -> uint256: view
    def precisions() -> uint256[2]: view
    def fee_calc(xp: uint256[2]) -> uint256: view
    def MATH() -> Math: view
    def coins(arg0: uint256) -> address: view
    def last_prices() -> uint256: view
    def last_timestamp() -> uint256: view
    def initial_A_gamma() -> uint256: view
    def initial_A_gamma_time() -> uint256: view
    def future_A_gamma() -> uint256: view
    def future_A_gamma_time() -> uint256: view
    def unabsorbed_xcp() -> uint256: view
    def dead_xcp() -> uint256: view
    def donation_duration() -> uint256: view
    def max_donation_ratio() -> uint256: view
    def last_donation_absorb_timestamp() -> uint256: view
    def balances(arg0: uint256) -> uint256: view
    def D() -> uint256: view
    def xcp_profit() -> uint256: view
    def xcp_profit_a() -> uint256: view
    def virtual_price() -> uint256: view
    def packed_rebalancing_params() -> uint256: view
    def packed_fee_params() -> uint256: view
    def ADMIN_FEE() -> uint256: view
    def name() -> String[64]: view
    def symbol() -> String[32]: view
    def decimals() -> uint8: view
    def version() -> String[8]: view
    def balanceOf(arg0: address) -> uint256: view
    def allowance(arg0: address, arg1: address) -> uint256: view
    def totalSupply() -> uint256: view
    def calc_remove_liquidity(amount: uint256) -> uint256[N_COINS]: view


interface Math:
    def newton_D(
        ANN: uint256,
        gamma: uint256,
        x_unsorted: uint256[N_COINS],
        K0_prev: uint256
    ) -> uint256: view
    def get_y(
        ANN: uint256,
        gamma: uint256,
        x: uint256[N_COINS],
        D: uint256,
        i: uint256,
    ) -> uint256[2]: view
    def newton_y(
        ANN: uint256,
        gamma: uint256,
        x: uint256[N_COINS],
        D: uint256,
        i: uint256,
    ) -> uint256: view


N_COINS: constant(uint256) = 2
PRECISION: constant(uint256) = 10**18


@external
@view
def get_dy(
    i: uint256, j: uint256, dx: uint256, swap: address
) -> uint256:

    dy: uint256 = 0
    xp: uint256[N_COINS] = empty(uint256[N_COINS])

    # dy = (get_y(x + dx) - y) * (1 - fee)
    dy, xp = self._get_dy_nofee(i, j, dx, swap)
    dy -= staticcall Curve(swap).fee_calc(xp) * dy // 10**10

    return dy


@view
@external
def get_dx(
    i: uint256, j: uint256, dy: uint256, swap: address
) -> uint256:

    dx: uint256 = 0
    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    fee_dy: uint256 = 0
    _dy: uint256 = dy

    # for more precise dx (but never exact), increase num loops
    for k: uint256 in range(5):
        dx, xp = self._get_dx_fee(i, j, _dy, swap)
        fee_dy = staticcall Curve(swap).fee_calc(xp) * _dy // 10**10
        _dy = dy + fee_dy + 1

    return dx


@view
@external
def calc_withdraw_one_coin(
    token_amount: uint256, i: uint256, swap: address
) -> uint256:

    return self._calc_withdraw_one_coin(token_amount, i, swap)[0]


@internal
@pure
def _xcp(D: uint256, price_scale: uint256) -> uint256:
    return D * PRECISION // N_COINS // isqrt(PRECISION * price_scale)

@internal
@view
def _xp(
    balances: uint256[N_COINS],
    price_scale: uint256,
    pool: Curve
) -> uint256[N_COINS]:
    PRECISIONS: uint256[N_COINS] = staticcall pool.precisions()
    return [
        balances[0] * PRECISIONS[0],
        unsafe_div(balances[1] * PRECISIONS[1] * price_scale, PRECISION)
    ]


@internal
@view
def _is_ramping(pool: Curve) -> bool:
    return staticcall pool.future_A_gamma_time() > block.timestamp


@internal
@pure
def _D_from_xcp(xcp: uint256, price_scale: uint256) -> uint256:
    return xcp * N_COINS * isqrt(price_scale * PRECISION) // PRECISION


@view
@external
def calc_add_liquidity(
    amounts: uint256[N_COINS],
    pool: Curve
) -> uint256:
    math: Math = staticcall pool.MATH()
    assert amounts[0] + amounts[1] > 0, "no coins to add"

    # --------------------- Get prices, balances -----------------------------

    old_balances: uint256[N_COINS] = [
        staticcall pool.balances(0),
        staticcall pool.balances(1)
    ]

    balances: uint256[N_COINS] = [
        staticcall pool.balances(0) + amounts[0],
        staticcall pool.balances(1) + amounts[1]
    ]

    price_scale: uint256 = staticcall pool.price_scale()
    xp: uint256[N_COINS] = self._xp(balances, price_scale, pool)
    old_xp: uint256[N_COINS] = self._xp(old_balances, price_scale, pool)

    # amountsp (amounts * p) contains the scaled `amounts_received` of each coin.
    amountsp: uint256[N_COINS] = empty(uint256[N_COINS])
    for i: uint256 in range(N_COINS):
        if amounts[i] > 0:
            amountsp[i] = xp[i] - old_xp[i]
    # -------------------- Calculate LP tokens to mint -----------------------

    A_gamma: uint256[2] = [
        staticcall pool.A(),
        staticcall pool.gamma()
    ]

    old_D: uint256 = 0
    if self._is_ramping(pool):
        # Recalculate D if A and/or gamma are ramping because the shape of
        # the bonding curve is changing.
        old_D = staticcall math.newton_D(A_gamma[0], A_gamma[1], old_xp, 0)
    else:
        old_D = staticcall pool.D()

    D: uint256 = staticcall math.newton_D(A_gamma[0], A_gamma[1], xp, 0)

    donation_D: uint256 = self._D_from_xcp(staticcall pool.dead_xcp(), price_scale)
    adjusted_D: uint256 = D - donation_D
    adjusted_old_D: uint256 = old_D - donation_D


    token_supply: uint256 = staticcall pool.totalSupply()
    d_token: uint256 = 0
    if old_D > 0:
        d_token = token_supply * adjusted_D // adjusted_old_D - token_supply
    else:
        d_token = self._xcp(adjusted_D, price_scale)  # <----- Making initial virtual price equal to 1.

    assert d_token > 0, "nothing minted"

    d_token_fee: uint256 = 0
    if adjusted_old_D > 0:

        d_token_fee = (
            staticcall pool.calc_token_fee(amountsp, xp) * d_token // 10**10 + 1
        )

        d_token -= d_token_fee

    return d_token


@external
@view
def calc_remove_liquidity(lp_tokens: uint256, pool: Curve) -> uint256[N_COINS]:
    return staticcall pool.calc_remove_liquidity(lp_tokens)


@external
@view
def calc_donate(amounts: uint256[N_COINS], pool: Curve) -> uint256:
    math: Math = staticcall pool.MATH()
    assert amounts[0] + amounts[1] > 0, "no coins to donate"

    balances: uint256[N_COINS] = [
        staticcall pool.balances(0),
        staticcall pool.balances(1)
    ]
    assert balances[0] + balances[1] > 0, "empty pool"

    price_scale: uint256 = staticcall pool.price_scale()
    A_gamma: uint256[2] = [
        staticcall pool.A(),
        staticcall pool.gamma()
    ]

    old_D: uint256 = 0
    if self._is_ramping(pool):
        # Recalculate D if A and/or gamma are ramping because the shape of
        # the bonding curve is changing.
        old_xp: uint256[N_COINS] = self._xp(balances, price_scale, pool)
        old_D = staticcall math.newton_D(A_gamma[0], A_gamma[1], old_xp, 0)
    else:
        old_D = staticcall pool.D()

    # TODO is this necessary?
    assert old_D > 0, "empty pool"

    for i: uint256 in range(N_COINS):
        if amounts[i] > 0:
            # This call changed self.balances
            balances[i] += amounts[i]

    xp: uint256[N_COINS] = self._xp(balances, price_scale, pool)

    # We recompute D to reflect the new balances. The donation is effectively
    # already available as liquidity for exchanges. However it will inflate
    # the virtual price only after the donation is absorbed.
    D: uint256 = staticcall math.newton_D(A_gamma[0], A_gamma[1], xp, 0)

    assert D > old_D, "donation caused loss"
    return D


@view
@external
def calc_token_amount(
    amounts: uint256[N_COINS], deposit: bool, swap: address, donation: bool = False
) -> uint256:

    d_token: uint256 = 0
    amountsp: uint256[N_COINS] = empty(uint256[N_COINS])
    xp: uint256[N_COINS] = empty(uint256[N_COINS])

    d_token, amountsp, xp = self._calc_dtoken_nofee(amounts, deposit, swap)
    if not donation:
        d_token -= (
            staticcall Curve(swap).calc_token_fee(amountsp, xp) * d_token // 10**10 + 1
        )

    return d_token


@external
@view
def calc_fee_get_dy(i: uint256, j: uint256, dx: uint256, swap: address
) -> uint256:

    dy: uint256 = 0
    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    dy, xp = self._get_dy_nofee(i, j, dx, swap)

    return (staticcall Curve(swap).fee_calc(xp)) * dy // 10**10


@external
@view
def calc_fee_withdraw_one_coin(
    token_amount: uint256, i: uint256, swap: address
) -> uint256:

    return self._calc_withdraw_one_coin(token_amount, i, swap)[1]


@view
@external
def calc_fee_token_amount(
    amounts: uint256[N_COINS], deposit: bool, swap: address
) -> uint256:

    d_token: uint256 = 0
    amountsp: uint256[N_COINS] = empty(uint256[N_COINS])
    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    d_token, amountsp, xp = self._calc_dtoken_nofee(amounts, deposit, swap)

    return (staticcall Curve(swap).calc_token_fee(amountsp, xp)) * d_token // 10**10 + 1


@internal
@view
def _calc_D_ramp(
    A: uint256,
    gamma: uint256,
    xp: uint256[N_COINS],
    precisions: uint256[N_COINS],
    price_scale: uint256,
    swap: address
) -> uint256:

    math: Math = staticcall Curve(swap).MATH()
    D: uint256 = staticcall Curve(swap).D()
    if staticcall Curve(swap).future_A_gamma_time() > block.timestamp:
        _xp: uint256[N_COINS] = xp
        _xp[0] *= precisions[0]
        _xp[1] = _xp[1] * price_scale * precisions[1] // PRECISION
        D = staticcall math.newton_D(A, gamma, _xp, 0)

    return D


@internal
@view
def _get_dx_fee(
    i: uint256, j: uint256, dy: uint256, swap: address
) -> (uint256, uint256[N_COINS]):

    # here, dy must include fees (and 1 wei offset)

    assert i != j and i < N_COINS and j < N_COINS, "coin index out of range"
    assert dy > 0, "do not exchange out 0 coins"

    math: Math = staticcall Curve(swap).MATH()

    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    precisions: uint256[N_COINS] = empty(uint256[N_COINS])
    price_scale: uint256 = 0
    D: uint256 = 0
    token_supply: uint256 = 0
    A: uint256 = 0
    gamma: uint256 = 0

    xp, D, token_supply, price_scale, A, gamma, precisions = self._prep_calc(swap)

    # adjust xp with output dy. dy contains fee element, which we handle later
    # (hence this internal method is called _get_dx_fee)
    xp[j] -= dy
    xp = [xp[0] * precisions[0], xp[1] * price_scale * precisions[1] // PRECISION]

    x_out: uint256[2] = staticcall math.get_y(A, gamma, xp, D, i)
    dx: uint256 = x_out[0] - xp[i]
    xp[i] = x_out[0]

    if i > 0:
        dx = dx * PRECISION // price_scale
    dx //= precisions[i]

    return dx, xp


@internal
@view
def _get_dy_nofee(
    i: uint256, j: uint256, dx: uint256, swap: address
) -> (uint256, uint256[N_COINS]):

    assert i != j and i < N_COINS and j < N_COINS, "coin index out of range"
    assert dx > 0, "do not exchange 0 coins"

    math: Math = staticcall Curve(swap).MATH()

    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    precisions: uint256[N_COINS] = empty(uint256[N_COINS])
    price_scale: uint256 = 0
    D: uint256 = 0
    token_supply: uint256 = 0
    A: uint256 = 0
    gamma: uint256 = 0

    xp, D, token_supply, price_scale, A, gamma, precisions = self._prep_calc(swap)

    # adjust xp with input dx
    xp[i] += dx
    xp = [
        xp[0] * precisions[0],
        xp[1] * price_scale * precisions[1] // PRECISION
    ]

    y_out: uint256[2] = staticcall math.get_y(A, gamma, xp, D, j)

    dy: uint256 = xp[j] - y_out[0] - 1
    xp[j] = y_out[0]
    if j > 0:
        dy = dy * PRECISION // price_scale
    dy //= precisions[j]

    return dy, xp


@internal
@view
def _calc_dtoken_nofee(
    amounts: uint256[N_COINS], deposit: bool, swap: address
) -> (uint256, uint256[N_COINS], uint256[N_COINS]):

    math: Math = staticcall Curve(swap).MATH()

    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    precisions: uint256[N_COINS] = empty(uint256[N_COINS])
    price_scale: uint256 = 0
    D0: uint256 = 0
    token_supply: uint256 = 0
    A: uint256 = 0
    gamma: uint256 = 0

    xp, D0, token_supply, price_scale, A, gamma, precisions = self._prep_calc(swap)

    amountsp: uint256[N_COINS] = amounts
    if deposit:
        for k: uint256 in range(N_COINS):
            xp[k] += amounts[k]
    else:
        for k: uint256 in range(N_COINS):
            xp[k] -= amounts[k]

    xp = [
        xp[0] * precisions[0],
        xp[1] * price_scale * precisions[1] // PRECISION
    ]
    amountsp = [
        amountsp[0]* precisions[0],
        amountsp[1] * price_scale * precisions[1] // PRECISION
    ]

    D: uint256 = staticcall math.newton_D(A, gamma, xp, 0)
    d_token: uint256 = token_supply * D // D0

    if deposit:
        d_token -= token_supply
    else:
        d_token = token_supply - d_token

    return d_token, amountsp, xp


@internal
@view
def _calc_withdraw_one_coin(
    token_amount: uint256,
    i: uint256,
    swap: address
) -> (uint256, uint256):

    token_supply: uint256 = staticcall Curve(swap).totalSupply()
    assert token_amount <= token_supply, "token amount more than supply"
    assert i < N_COINS, "coin out of range"

    math: Math = staticcall Curve(swap).MATH()

    xx: uint256[N_COINS] = empty(uint256[N_COINS])
    for k: uint256 in range(N_COINS):
        xx[k] = staticcall Curve(swap).balances(k)

    precisions: uint256[N_COINS] = staticcall Curve(swap).precisions()
    A: uint256 = staticcall Curve(swap).A()
    gamma: uint256 = staticcall Curve(swap).gamma()
    D0: uint256 = 0
    p: uint256 = 0

    price_scale_i: uint256 = staticcall Curve(swap).price_scale() * precisions[1]
    xp: uint256[N_COINS] = [
        xx[0] * precisions[0],
        unsafe_div(xx[1] * price_scale_i, PRECISION)
    ]
    if i == 0:
        price_scale_i = PRECISION * precisions[0]

    if staticcall Curve(swap).future_A_gamma_time() > block.timestamp:
        D0 = staticcall math.newton_D(A, gamma, xp, 0)
    else:
        D0 = staticcall Curve(swap).D()

    D: uint256 = D0

    fee: uint256 = self._fee(xp, swap)
    dD: uint256 = token_amount * D // token_supply

    D_fee: uint256 = fee * dD // (2 * 10**10) + 1
    approx_fee: uint256 = N_COINS * D_fee * xx[i] // D

    D -= (dD - D_fee)

    y_out: uint256[2] = staticcall math.get_y(A, gamma, xp, D, i)
    dy: uint256 = (xp[i] - y_out[0]) * PRECISION // price_scale_i
    xp[i] = y_out[0]

    return dy, approx_fee


@internal
@view
def _fee(xp: uint256[N_COINS], swap: address) -> uint256:

    packed_fee_params: uint256 = staticcall Curve(swap).packed_fee_params()
    fee_params: uint256[3] = self._unpack_3(packed_fee_params)
    f: uint256 = xp[0] + xp[1]
    f = fee_params[2] * 10**18 // (
        fee_params[2] + 10**18 -
        (10**18 * N_COINS**N_COINS) * xp[0] // f * xp[1] // f
    )

    return (fee_params[0] * f + fee_params[1] * (10**18 - f)) // 10**18


@internal
@view
def _prep_calc(swap: address) -> (
    uint256[N_COINS],
    uint256,
    uint256,
    uint256,
    uint256,
    uint256,
    uint256[N_COINS]
):

    precisions: uint256[N_COINS] = staticcall Curve(swap).precisions()
    token_supply: uint256 = staticcall Curve(swap).totalSupply()
    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    for k: uint256 in range(N_COINS):
        xp[k] = staticcall Curve(swap).balances(k)

    price_scale: uint256 = staticcall Curve(swap).price_scale()

    A: uint256 = staticcall Curve(swap).A()
    gamma: uint256 = staticcall Curve(swap).gamma()
    D: uint256 = self._calc_D_ramp(
        A, gamma, xp, precisions, price_scale, swap
    )

    return xp, D, token_supply, price_scale, A, gamma, precisions


@internal
@view
def _unpack_3(_packed: uint256) -> uint256[3]:
    """
    @notice Unpacks a uint256 into 3 integers (values must be <= 10**18)
    @param val The uint256 to unpack
    @return The unpacked uint256[3]
    """
    return [
        (_packed >> 128) & 18446744073709551615,
        (_packed >> 64) & 18446744073709551615,
        _packed & 18446744073709551615,
    ]
