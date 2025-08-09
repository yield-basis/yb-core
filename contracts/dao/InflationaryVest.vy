# @version 0.4.3
"""
@title InflationaryVest
@author Yield Basis
@license MIT
@notice Vests YB token for one address which can be changed by governance (admin),
        proportional to inflation.
"""
from snekmate.auth import ownable


interface YBToken:
    def reserve() -> uint256: view
    def balanceOf(user: address) -> uint256: view
    def transfer(_to: address, _amount: uint256) -> bool: nonpayable

event NewRecepient:
    recepient: address
    old_recepient: address

event Start:
    timestamp: uint256
    amount: uint256

event Claim:
    recepient: address
    claimed: uint256


owner: public(immutable(address))
YB: public(immutable(YBToken))
INITIAL_YB_RESERVE: public(immutable(uint256))
recepient: public(address)
claimed: public(uint256)

initial_vest_reserve: public(uint256)


@deploy
def __init__(yb: YBToken, recepient: address, admin: address):
    assert admin != empty(address)
    owner = admin
    YB = yb
    INITIAL_YB_RESERVE = staticcall YB.reserve()
    self.recepient = recepient


@external
def start():
    assert msg.sender == owner, "Admin required"
    assert self.initial_vest_reserve == 0, "Already started"
    vest_reserve: uint256 = staticcall YB.balanceOf(self)
    self.initial_vest_reserve = vest_reserve
    log Start(timestamp=block.timestamp, amount=vest_reserve)


@external
def set_recepient(new_recepient: address):
    assert msg.sender == owner, "Admin required"
    log NewRecepient(recepient=new_recepient, old_recepient=self.recepient)
    self.recepient = new_recepient


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
    recepient: address = self.recepient
    extcall YB.transfer(recepient, claimable)
    log Claim(recepient=recepient, claimed=claimable)
    return claimable
