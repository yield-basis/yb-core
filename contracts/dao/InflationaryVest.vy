# @version 0.4.3
"""
@title InflationaryVest
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Vests YB token for one address which can be changed by governance (admin),
        proportional to inflation.
"""
from snekmate.auth import ownable


initializes: ownable

exports: (
    ownable.transfer_ownership,
    ownable.owner
)

interface YBToken:
    def reserve() -> uint256: view
    def balanceOf(user: address) -> uint256: view
    def transfer(_to: address, _amount: uint256) -> bool: nonpayable

event NewRecepient:
    recipient: address
    old_recipient: address

event Start:
    timestamp: uint256
    amount: uint256

event Claim:
    recipient: address
    claimed: uint256


YB: public(immutable(YBToken))
INITIAL_YB_RESERVE: public(immutable(uint256))
recipient: public(address)
claimed: public(uint256)

initial_vest_reserve: public(uint256)


@deploy
def __init__(yb: YBToken, recipient: address, admin: address):
    assert admin != empty(address)
    ownable.__init__()
    ownable.owner = admin
    YB = yb
    INITIAL_YB_RESERVE = staticcall YB.reserve()
    self.recipient = recipient


@external
def start():
    assert msg.sender == ownable.owner, "Admin required"
    assert self.initial_vest_reserve == 0, "Already started"
    vest_reserve: uint256 = staticcall YB.balanceOf(self)
    self.initial_vest_reserve = vest_reserve
    log Start(timestamp=block.timestamp, amount=vest_reserve)


@external
def set_recipient(new_recipient: address):
    assert msg.sender == ownable.owner, "Admin required"
    log NewRecepient(recipient=new_recipient, old_recipient=self.recipient)
    self.recipient = new_recipient


@internal
@view
def _claimable() -> uint256:
    initial_vest_reserve: uint256 = self.initial_vest_reserve
    claimed: uint256 = self.claimed
    return (INITIAL_YB_RESERVE - staticcall YB.reserve()) * initial_vest_reserve // INITIAL_YB_RESERVE - self.claimed


@external
@view
def claimable() -> uint256:
    return self._claimable()


@external
def claim() -> uint256:
    claimable: uint256 = self._claimable()
    recipient: address = self.recipient
    extcall YB.transfer(recipient, claimable)
    log Claim(recipient=recipient, claimed=claimable)
    return claimable
