# @version 0.4.0
"""
@title LToken - AMM for leverage
@author Michael Egorov
@license Copyright (c)
"""

interface IERC20:
    def decimals() -> uint256: view

LEVERAGE: public(immutable(uint256))
COLLATERAL: public(immutable(IERC20))
STABLECOIN: public(immutable(IERC20))

COLLATERAL_PRECISION: immutable(uint256)
# Stablecoin precision is always 1e18

fee: public(uint256)
admin: public(address)


event SetAdmin:
    admin: address


@deploy
def __init__(stablecoin: IERC20, collateral: IERC20, leverage: uint256, fee: uint256, admin: address):
    STABLECOIN = stablecoin
    COLLATERAL = collateral
    LEVERAGE = leverage
    self.fee = fee
    self.admin = admin

    COLLATERAL_PRECISION = 10**(18 - staticcall COLLATERAL.decimals())


@external
@view
def coins(i: uint256) -> IERC20:
    return [STABLECOIN, COLLATERAL][i]


@external
def set_admin(new_admin: address):
    assert msg.sender == self.admin
    self.admin = new_admin
    log SetAdmin(new_admin)
