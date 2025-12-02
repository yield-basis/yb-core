# @version 0.4.3
"""
@title TokenSender
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Very simple contract to take all specified tokens from the sender and send it to the target
"""
from ethereum.ercs import IERC20


TOKENS: public(immutable(DynArray[IERC20, 20]))
TARGET: public(immutable(address))


@deploy
def __init__(target: address, tokens: DynArray[IERC20, 20]):
    TARGET = target
    TOKENS = tokens


@external
def send():
    for token: IERC20 in TOKENS:
        amount: uint256 = staticcall token.balanceOf(msg.sender)
        assert extcall token.transferFrom(msg.sender, TARGET, amount, default_return_value=True)
