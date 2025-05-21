# @version 0.4.1
"""
@title crvUSD Fake FlashLender
@notice ERC3156 contract for crvUSD flash loans
"""

version: public(constant(String[8])) = "TEST"

from ethereum.ercs import IERC20 as ERC20

interface ERC3156FlashBorrower:
    def onFlashLoan(initiator: address, token: address, amount: uint256, fee: uint256, data: Bytes[10**5]): nonpayable


event FlashLoan:
    caller: indexed(address)
    receiver: indexed(address)
    amount: uint256


CRVUSD: immutable(address)
fee: public(constant(uint256)) = 0  # 1 == 0.01 %


@deploy
def __init__(crvusd: address):
    CRVUSD = crvusd
    extcall ERC20(CRVUSD).approve(msg.sender, max_value(uint256))


@external
@view
def supportedTokens(token: address) -> bool:
    return token == CRVUSD


@external
@nonreentrant
def flashLoan(receiver: ERC3156FlashBorrower, token: address, amount: uint256, data: Bytes[10**5]) -> bool:
    """
    @notice Loan `amount` tokens to `receiver`, and takes it back plus a `flashFee` after the callback
    @param receiver The contract receiving the tokens, needs to implement the
    `onFlashLoan(initiator: address, token: address, amount: uint256, fee: uint256, data: Bytes[10**5])` interface.
    @param token The loan currency.
    @param amount The amount of tokens lent.
    @param data A data parameter to be passed on to the `receiver` for any custom use.
    """
    assert token == CRVUSD, "FlashLender: Unsupported currency"
    ceiling: uint256 = staticcall ERC20(CRVUSD).balanceOf(self)
    extcall ERC20(CRVUSD).transfer(receiver.address, amount)
    extcall receiver.onFlashLoan(msg.sender, CRVUSD, amount, 0, data)
    assert staticcall ERC20(CRVUSD).balanceOf(self) >= ceiling, "FlashLender: Repay failed"

    log FlashLoan(caller=msg.sender, receiver=receiver.address, amount=amount)

    return True


@external
@view
def flashFee(token: address, amount: uint256) -> uint256:
    """
    @notice The fee to be charged for a given loan.
    @param token The loan currency.
    @param amount The amount of tokens lent.
    @return The amount of `token` to be charged for the loan, on top of the returned principal.
    """
    assert token == CRVUSD, "FlashLender: Unsupported currency"
    return 0


@external
@view
def maxFlashLoan(token: address) -> uint256:
    """
    @notice The amount of currency available to be lent.
    @param token The loan currency.
    @return The amount of `token` that can be borrowed.
    """
    if token == CRVUSD:
        return staticcall ERC20(CRVUSD).balanceOf(self)
    else:
        return 0
