# @version 0.4.0
"""
@title LEVAMM
@notice Automatic market maker which keeps constant leverage
@author Michael Egorov
@license Copyright (c)
"""

interface IERC20:
    def decimals() -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view


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

    extcall stablecoin.approve(DEPOSITOR, max_value(uint256))
    extcall collateral.approve(DEPOSITOR, max_value(uint256))


# Math
@internal
@view
def get_x0(p_oracle: uint256, collateral: uint256, debt: uint256) -> uint256:
    coll_value: uint256 = p_oracle * collateral * COLLATERAL_PRECISION // 10**18
    D: uint256 = coll_value**2 - 4 * coll_value * LEV_RATIO // 10**18 * debt
    return (coll_value + isqrt(D)) * 10**18 // (2 * LEV_RATIO)
###


@external
@view
def get_dy(i: uint256, j: uint256, in_amount: uint256) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)

    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self.debt
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
    debt: uint256 = self.debt
    return (self.get_x0(p_o, collateral, debt) - self.debt) * (10**18 // COLLATERAL_PRECISION) // self.collateral_amount


@external
def exchange(i: uint256, j: uint256, in_amount: uint256, _for: address = msg.sender) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)

    p_o: uint256 = staticcall PRICE_ORACLE_CONTRACT.price()
    collateral: uint256 = self.collateral_amount  # == y_initial
    debt: uint256 = self.debt
    x_initial: uint256 = self.get_x0(p_o, collateral, debt) - debt

    out_amount: uint256 = 0

    if i == 0:  # Buy collateral
        x: uint256 = x_initial + in_amount
        y: uint256 = x_initial * collateral // x
        out_amount = (collateral - y) * (10**18 - self.fee) // 10**18
        self.debt -= in_amount
        self.collateral_amount -= out_amount
        assert extcall STABLECOIN.transferFrom(msg.sender, self, in_amount, default_return_value=True)
        assert extcall COLLATERAL.transfer(_for, out_amount, default_return_value=True)

    else:  # Sell collateral
        y: uint256 = collateral + in_amount
        x: uint256 = x_initial * collateral // y
        out_amount = (x_initial - x) * (10**18 - self.fee) // 10**18
        self.debt += out_amount
        self.collateral_amount += in_amount
        assert extcall COLLATERAL.transferFrom(msg.sender, self, in_amount, default_return_value=True)
        assert extcall STABLECOIN.transfer(_for, out_amount, default_return_value=True)

    return out_amount


@external
def _borrow(amount: uint256):
    pass


@external
def _deposit(collateral_amount: uint256, borrowed_amount: uint256, min_invariant_change: uint256):
    pass


@external
@view
def coins(i: uint256) -> IERC20:
    return [STABLECOIN, COLLATERAL][i]


@external
@view
def invariant(collateral_amount: uint256, borrowed_amount: uint256) -> uint256:
    return 0


@external
@view
def invariant_change(collateral_amount: uint256, borrowed_amount: uint256) -> uint256:
    return 0
