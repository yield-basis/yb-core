# @version 0.4.0
"""
@title LiqLevToken
@notice AMM for leveraging 2-token liquidity
@author Michael Egorov
@license Copyright (c) 2024
"""

interface IERC20:
    def decimals() -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable


interface LevAMM:
    def _deposit(d_collateral: uint256, d_debt: uint256, min_invariant_change: uint256) -> uint256: nonpayable
    def _withdraw(invariant_change: uint256, min_collateral_return: uint256, max_debt_return: uint256) -> uint256[2]: nonpayable
    def invariant_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> uint256: view


COLLATERAL: public(immutable(IERC20))  # Liquidity like LP(TBTC/crvUSD)
STABLECOIN: public(immutable(IERC20))  # For example, crvUSD
DEPOSITED_TOKEN: public(immutable(IERC20))  # For example, TBTC

admin: public(address)
amm: public(address)

event SetAdmin:
    admin: address


allowance: public(HashMap[address, HashMap[address, uint256]])
balanceOf: public(HashMap[address, uint256])
totalSupply: public(uint256)


@deploy
def __init__(deposited_token: IERC20, stablecoin: IERC20, collateral: IERC20,
             admin: address):
    """
    @notice Initializer (can be performed by an EOA deployer or a factory)
    @param deposited_token Token which gets deposited. Can be collateral or can be not
    @param stablecoin Stablecoin which gets "granted" to this contract to use for loans. Has to be 18 decimals
    @param collateral Collateral token
    @param admin Admin which can set callbacks, stablecoin allocator and fee. Sensitive!
    """
    # Example:
    # deposit_token = WBTC
    # stablecoin = crvUSD
    # collateral = WBTC LP

    STABLECOIN = stablecoin
    COLLATERAL = collateral
    DEPOSITED_TOKEN = deposited_token
    self.admin = admin


@external
@nonreentrant
def set_amm(amm: address):
    assert msg.sender == self.admin, "Access"
    self.amm = amm


@external
@nonreentrant
def set_admin(new_admin: address):
    assert msg.sender == self.admin, "Access"
    self.admin = new_admin
    log SetAdmin(new_admin)
