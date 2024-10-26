# @version 0.4.0
"""
@title LToken - AMM for leverage
@author Michael Egorov
@license Copyright (c)
"""

interface IERC20:
    def decimals() -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable

LEVERAGE: public(immutable(uint256))
COLLATERAL: public(immutable(IERC20))
STABLECOIN: public(immutable(IERC20))
DEPOSITED_TOKEN: public(immutable(IERC20))

COLLATERAL_PRECISION: immutable(uint256)
# Stablecoin precision is always 1e18

fee: public(uint256)
admin: public(address)
stablecoin_allocator: public(address)


event SetAdmin:
    admin: address

event SetAllocator:
    allocator: address


@deploy
def __init__(deposited_token: IERC20, stablecoin: IERC20, collateral: IERC20, leverage: uint256, fee: uint256, admin: address):
    """
    @notice Initializer (can be performed by an EOA deployer or a factory)
    @param deposited_token Token which gets deposited. Can be collateral or can be not
    @param stablecoin Stablecoin which gets "granted" to this contract to use for loans. Has to be 18 decimals
    @param collateral Collateral token
    @param leverage Degree of leverage, 1e18-based
    @param fee Fee of the AMM, 1e10-based
    @param admin Admin which can set callbacks, stablecoin allocator and fee. Sensitive!
    """
    # Example:
    # deposit_token = WBTC
    # stablecoin = crvUSD
    # collateral = WBTC LP

    STABLECOIN = stablecoin
    COLLATERAL = collateral
    DEPOSITED_TOKEN = deposited_token
    LEVERAGE = leverage
    self.fee = fee
    self.admin = admin

    COLLATERAL_PRECISION = 10**(18 - staticcall COLLATERAL.decimals())
    assert staticcall STABLECOIN.decimals() == 18


@external
@view
def coins(i: uint256) -> IERC20:
    return [STABLECOIN, COLLATERAL][i]


@external
@nonreentrant
def set_stablecoin_allocator(allocator: address):
    # Allocator is an address which can take ALL the stablecoins back
    # Therefore, it has to be set cautiously
    assert msg.sender == self.admin
    if self.stablecoin_allocator == empty(address):
        extcall STABLECOIN.approve(self.stablecoin_allocator, 0)
    self.stablecoin_allocator = allocator
    extcall STABLECOIN.approve(allocator, max_value(uint256))
    log SetAllocator(allocator)


@external
@nonreentrant
def set_admin(new_admin: address):
    assert msg.sender == self.admin
    self.admin = new_admin
    log SetAdmin(new_admin)
