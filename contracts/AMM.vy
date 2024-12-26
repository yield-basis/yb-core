# @version 0.4.0
"""
@title LEVAMM
@notice Automatic market maker which keeps constant leverage
@author Michael Egorov
@license Copyright (c) 2024
"""

interface IERC20:
    def decimals() -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view


struct AMMState:
    collateral: uint256
    debt: uint256
    x0: uint256


LEVERAGE: public(immutable(uint256))
LEV_RATIO: immutable(uint256)
DEPOSITOR: public(immutable(address))
COLLATERAL: public(immutable(IERC20))
STABLECOIN: public(immutable(IERC20))
PRICE_ORACLE_CONTRACT: public(immutable(PriceOracle))

COLLATERAL_PRECISION: immutable(uint256)

fee: public(uint256)

collateral_amount: public(uint256)
debt: public(uint256)
rate: public(uint256)
rate_mul: public(uint256)
rate_time: uint256

minted: public(uint256)
redeemed: public(uint256)


event TokenExchange:
    buyer: indexed(address)
    sold_id: uint256
    tokens_sold: uint256
    bought_id: uint256
    tokens_bought: uint256
    fee: uint256
    price_oracle: uint256

event AddLiquidityRaw:
    token_amounts: uint256[2]
    invariant: uint256
    price_oracle: uint256

event RemoveLiquidityRaw:
    collateral_change: uint256
    debt_change: uint256

event SetRate:
    rate: uint256
    rate_mul: uint256
    time: uint256

event CollectFees:
    amount: uint256
    new_supply: uint256


@deploy
def __init__(depositor: address,
             stablecoin: IERC20, collateral: IERC20, leverage: uint256,
             fee: uint256, price_oracle_contract: PriceOracle):
    DEPOSITOR = depositor
    STABLECOIN = stablecoin
    COLLATERAL = collateral
    LEVERAGE = leverage
    self.fee = fee
    PRICE_ORACLE_CONTRACT = price_oracle_contract

    COLLATERAL_PRECISION = 10**(18 - staticcall COLLATERAL.decimals())
    assert staticcall STABLECOIN.decimals() == 18
    assert leverage > 10**18

    denominator: uint256 = 2 * leverage - 1
    LEV_RATIO = leverage**2 // denominator * 10**18 // denominator

    self.rate_mul = 10**18
    self.rate_time = block.timestamp

    extcall stablecoin.approve(DEPOSITOR, max_value(uint256))
    extcall collateral.approve(DEPOSITOR, max_value(uint256))


# Math
@internal
@pure
def sqrt(arg: uint256) -> uint256:
    return isqrt(arg)


@internal
@view
def get_x0(p_oracle: uint256, collateral: uint256, debt: uint256) -> uint256:
    coll_value: uint256 = p_oracle * collateral * COLLATERAL_PRECISION // 10**18
    D: uint256 = coll_value**2 - 4 * coll_value * LEV_RATIO // 10**18 * debt
    return (coll_value + self.sqrt(D)) * 10**18 // (2 * LEV_RATIO)
###


@internal
@view
def _rate_mul() -> uint256:
    """
    @notice Rate multiplier which is 1.0 + integral(rate, dt)
    @return Rate multiplier in units where 1.0 == 1e18
    """
    return unsafe_div(self.rate_mul * (10**18 + self.rate * (block.timestamp - self.rate_time)), 10**18)


@external
@view
def get_rate_mul() -> uint256:
    """
    @notice Rate multiplier which is 1.0 + integral(rate, dt)
    @return Rate multiplier in units where 1.0 == 1e18
    """
    return self._rate_mul()


@external
@nonreentrant
def set_rate(rate: uint256) -> uint256:
    """
    @notice Set interest rate. That affects the dependence of AMM base price over time
    @param rate New rate in units of int(fraction * 1e18) per second
    @return rate_mul multiplier (e.g. 1.0 + integral(rate, dt))
    """
    assert msg.sender == DEPOSITOR, "Access"
    rate_mul: uint256 = self._rate_mul()
    self.rate_mul = rate_mul
    self.rate_time = block.timestamp
    self.rate = rate
    log SetRate(rate, rate_mul, block.timestamp)
    return rate_mul


@internal
@view
def _debt() -> uint256:
    return self.debt * self._rate_mul() // self.rate_mul


@internal
def _debt_w() -> uint256:
    rate_mul: uint256 = self._rate_mul()
    debt: uint256 = self.debt * rate_mul // self.rate_mul
    self.rate_mul = rate_mul
    self.rate_time = block.timestamp
    return debt


@external
@view
def get_debt() -> uint256:
    return self._debt()


@external
@view
def get_state() -> AMMState:
    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    state: AMMState = empty(AMMState)
    state.collateral = self.collateral_amount
    state.debt = self._debt()
    state.x0 = self.get_x0(p_o, state.collateral, state.debt)
    return state


@external
@view
def get_dy(i: uint256, j: uint256, in_amount: uint256) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)

    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self._debt()
    x_initial: uint256 = self.get_x0(p_o, collateral, debt) - debt

    if i == 0:  # Buy collateral
        x: uint256 = x_initial + in_amount
        y: uint256 = x_initial * collateral // x
        return (collateral - y) * (10**18 - self.fee) // 10**18

    else:  # Sell collateral
        y: uint256 = collateral + in_amount
        x: uint256 = x_initial * collateral // y
        return (x_initial - x) * (10**18 - self.fee) // 10**18


@external
@view
def get_p() -> uint256:
    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount
    debt: uint256 = self._debt()
    return (self.get_x0(p_o, collateral, debt) - self.debt) * (10**18 // COLLATERAL_PRECISION) // self.collateral_amount


@external
@nonreentrant
def exchange(i: uint256, j: uint256, in_amount: uint256, _for: address = msg.sender) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)

    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self._debt_w()
    x_initial: uint256 = self.get_x0(p_o, collateral, debt) - debt

    out_amount: uint256 = 0
    fee: uint256 = self.fee

    if i == 0:  # Trader buys collateral from us
        x: uint256 = x_initial + in_amount
        y: uint256 = x_initial * collateral // x
        out_amount = (collateral - y) * (10**18 - fee) // 10**18
        self.debt -= in_amount
        self.collateral_amount -= out_amount
        self.redeemed += in_amount
        assert extcall STABLECOIN.transferFrom(msg.sender, self, in_amount, default_return_value=True)
        assert extcall COLLATERAL.transfer(_for, out_amount, default_return_value=True)

    else:  # Trader sells collateral to us
        y: uint256 = collateral + in_amount
        x: uint256 = x_initial * collateral // y
        out_amount = (x_initial - x) * (10**18 - fee) // 10**18
        self.debt += out_amount
        self.minted += out_amount
        self.collateral_amount += in_amount
        assert extcall COLLATERAL.transferFrom(msg.sender, self, in_amount, default_return_value=True)
        assert extcall STABLECOIN.transfer(_for, out_amount, default_return_value=True)

    log TokenExchange(msg.sender, i, in_amount, j, out_amount, fee, p_o)

    return out_amount


@external
def _deposit(d_collateral: uint256, d_debt: uint256) -> uint256[2]:
    assert msg.sender == DEPOSITOR, "Access violation"

    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self._debt_w()
    x0: uint256 = self.get_x0(p_o, collateral, debt)

    value_before: uint256 = 2 * self.sqrt((x0 - debt) * collateral * COLLATERAL_PRECISION * p_o // 10**18) - x0

    debt += d_debt
    collateral += d_collateral
    self.minted += d_debt

    self.debt = debt
    self.collateral_amount = collateral
    # Assume that transfer of collateral happened already (as a result of exchange)

    value_after: uint256 = 2 * self.sqrt((x0 - debt) * collateral * COLLATERAL_PRECISION * p_o // 10**18) - x0

    log AddLiquidityRaw([d_collateral, d_debt], value_after, p_o)
    return [value_before, value_after]


@external
def _withdraw(frac: uint256) -> uint256[2]:
    assert msg.sender == DEPOSITOR, "Access violation"

    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self._debt_w()

    d_collateral: uint256 = collateral * frac // 10**18
    d_debt: uint256 = debt * frac // 10**18

    self.collateral_amount -= d_collateral
    self.debt = debt - d_debt
    self.redeemed += d_debt

    log RemoveLiquidityRaw(d_collateral, d_debt)

    return [d_collateral, d_debt]


@external
@view
def coins(i: uint256) -> IERC20:
    return [STABLECOIN, COLLATERAL][i]


@external
@view
def value_oracle() -> uint256:
    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self._debt()
    x0: uint256 = self.get_x0(p_o, collateral, debt)
    Ip: uint256 = self.sqrt((x0 - debt) * collateral * COLLATERAL_PRECISION * p_o // 10**18)
    return 2 * Ip - x0


@external
@view
def value_oracle_for(collateral: uint256, debt: uint256) -> uint256:
    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    x0: uint256 = self.get_x0(p_o, collateral, debt)
    Ip: uint256 = self.sqrt((x0 - debt) * collateral * COLLATERAL_PRECISION * p_o // 10**18)
    return 2 * Ip - x0


@external
@view
def get_invariant() -> uint256:
    collateral: uint256 = self.collateral_amount  # == y_initial
    if collateral == 0:
        return 0
    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    debt: uint256 = self._debt()
    x0: uint256 = self.get_x0(p_o, collateral, debt)
    return self.sqrt((x0 - debt) * collateral * COLLATERAL_PRECISION)


@external
@view
def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> uint256[2]:
    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self._debt()
    x0: uint256 = self.get_x0(p_o, collateral, debt)
    val_before: uint256 = 2 * self.sqrt((x0 - debt) * collateral * COLLATERAL_PRECISION * p_o // 10**18) - x0
    val_after: uint256 = 0
    if is_deposit:
        val_after = 2 * self.sqrt(
            (collateral + collateral_amount) * COLLATERAL_PRECISION * (x0 - (debt + borrowed_amount)) * p_o // 10**18
        ) - x0
    else:
        val_after = 2 * self.sqrt(
            (collateral - collateral_amount) * COLLATERAL_PRECISION * (x0 - (debt - borrowed_amount)) * p_o // 10**18
        ) - x0
    return [val_before, val_after]


@external
@view
def admin_fees() -> uint256:
    """
    @notice Calculate the amount of fees obtained from the interest
    """
    minted: uint256 = self.minted
    return unsafe_sub(max(self._debt() + self.redeemed, minted), minted)


@external
@nonreentrant
def collect_fees() -> uint256:
    """
    @notice Collect the fees charged as interest.
    """
    debt: uint256 = self._debt_w()
    minted: uint256 = self.minted
    to_be_redeemed: uint256 = debt + self.redeemed
    # Difference between to_be_redeemed and minted amount is exactly due to interest charged
    if to_be_redeemed > minted:
        self.minted = to_be_redeemed
        to_be_redeemed = unsafe_sub(to_be_redeemed, minted)  # Now this is the fees to charge
        extcall STABLECOIN.transfer(DEPOSITOR, to_be_redeemed)
        log CollectFees(to_be_redeemed, debt)
        return to_be_redeemed
    else:
        log CollectFees(0, debt)
        return 0
