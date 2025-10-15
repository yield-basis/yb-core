# @version 0.4.3
"""
@title Multisend
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Sends tokens if they were not sent yet, single-use only
"""
from ethereum.ercs import IERC20


TOKEN: public(immutable(IERC20))
already_sent: public(HashMap[address, bool])
ADMIN: public(immutable(address))


@deploy
def __init__(token: IERC20):
    TOKEN = token
    ADMIN = msg.sender


@external
def send(users: DynArray[address, 500], amounts: DynArray[uint256, 500]):
    assert msg.sender == ADMIN  # otherwise someone could set the already_sent!

    i: uint256 = 0
    for user: address in users:
        if not self.already_sent[user]:
            amount: uint256 = amounts[i]
            extcall TOKEN.transferFrom(msg.sender, user, amount)
            self.already_sent[user] = True
        i += 1
