# @version 0.4.1
"""
@title Cliff Escrow
@author Yield Basis
@license MIT
@notice Limits what one can do with received tokens before the cliff time is over
"""
from ethereum.ercs import IERC20


interface VotingEscrow:
    def create_lock(_value: uint256, _unlock_time: uint256): nonpayable
    def increase_amount(_value: uint256): nonpayable
    def increase_unlock_time(_unlock_time: uint256): nonpayable
    def withdraw(): nonpayable
    def transferFrom(owner: address, to: address, token_id: uint256): nonpayable

interface GaugeController:
    def vote_for_gauge_weights(_gauge_addrs: DynArray[address, 50], _user_weights: DynArray[uint256, 50]): nonpayable


GC: public(immutable(GaugeController))
YB: public(immutable(IERC20))
VE: public(immutable(VotingEscrow))

UNLOCK_TIME: public(immutable(uint256))
RECEPIENT: public(immutable(address))


@deploy
def __init__(token: IERC20, unlock_time: uint256, ve: VotingEscrow, gc: GaugeController, recepient: address):
    RECEPIENT = recepient
    YB = token
    VE = ve
    GC = gc
    assert unlock_time > block.timestamp
    UNLOCK_TIME = unlock_time
    extcall token.approve(ve.address, max_value(uint256))


@internal
def _access():
    assert msg.sender == RECEPIENT, "Not authorized"


@internal
def _cliff():
    assert block.timestamp >= UNLOCK_TIME, "Cliff still applies"


@external
def create_lock(_value: uint256, _unlock_time: uint256):
    self._access()
    extcall VE.create_lock(_value, _unlock_time)


@external
def increase_amount(_value: uint256):
    self._access()
    extcall VE.increase_amount(_value)


@external
def increase_unlock_time(_unlock_time: uint256):
    self._access()
    extcall VE.increase_unlock_time(_unlock_time)


@external
def withdraw():
    self._access()
    extcall VE.withdraw()


@external
def transferFrom(owner: address, to: address, token_id: uint256):
    self._access()
    self._cliff()
    extcall VE.transferFrom(owner, to, token_id)


@external
def vote_for_gauge_weights(_gauge_addrs: DynArray[address, 50], _user_weights: DynArray[uint256, 50]):
    self._access()
    extcall GC.vote_for_gauge_weights(_gauge_addrs, _user_weights)


@external
def transfer(to: address, amount: uint256):
    self._access()
    self._cliff()
    extcall YB.transfer(to, amount)


@external
def approve(_for: address, amount: uint256):
    self._access()
    self._cliff()
    extcall YB.approve(_for, amount)


@external
def recover_token(token: IERC20, to: address, amount: uint256):
    self._access()
    assert token != YB, "Cannot recover YB"
    assert extcall token.transfer(to, amount, default_return_value=True)
