# @version 0.4.3
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
    def infinite_lock_toggle(): nonpayable

interface GaugeController:
    def vote_for_gauge_weights(_gauge_addrs: DynArray[address, 50], _user_weights: DynArray[uint256, 50]): nonpayable

interface AragonDAO:
    def vote(_proposalId: uint256, _voteOption: uint8, _tryEarlyExecution: bool): nonpayable


event TokenRecovered:
    token: indexed(address)
    to: address
    amount: uint256


GC: public(immutable(GaugeController))
YB: public(immutable(IERC20))
VE: public(immutable(VotingEscrow))

unlock_time: public(uint256)
recipient: public(address)


@deploy
def __init__(token: IERC20, ve: VotingEscrow, gc: GaugeController):
    YB = token
    VE = ve
    GC = gc
    self.recipient = self


@external
def initialize(recipient: address, unlock_time: uint256) -> bool:
    assert recipient != empty(address), "Empty recipient"
    assert self.recipient == empty(address), "Already initialized"
    assert unlock_time > block.timestamp
    self.recipient = recipient
    self.unlock_time = unlock_time
    extcall YB.approve(VE.address, max_value(uint256))
    return True


@internal
def _access():
    assert msg.sender == self.recipient, "Not authorized"


@internal
def _cliff():
    assert block.timestamp >= self.unlock_time, "Cliff still applies"


@external
@nonreentrant
def create_lock(_value: uint256, _unlock_time: uint256):
    self._access()
    extcall VE.create_lock(_value, _unlock_time)


@external
@nonreentrant
def increase_amount(_value: uint256):
    self._access()
    extcall VE.increase_amount(_value)


@external
@nonreentrant
def increase_unlock_time(_unlock_time: uint256):
    self._access()
    extcall VE.increase_unlock_time(_unlock_time)


@external
@nonreentrant
def withdraw():
    self._access()
    extcall VE.withdraw()


@external
@nonreentrant
def transferFrom(owner: address, to: address, token_id: uint256):
    self._access()
    self._cliff()
    extcall VE.transferFrom(owner, to, token_id)


@external
@nonreentrant
def vote_for_gauge_weights(_gauge_addrs: DynArray[address, 50], _user_weights: DynArray[uint256, 50]):
    self._access()
    extcall GC.vote_for_gauge_weights(_gauge_addrs, _user_weights)


@external
@nonreentrant
def aragon_vote(dao: AragonDAO, proposal_id: uint256, vote_option: uint8, early_execution: bool):
    self._access()
    extcall dao.vote(proposal_id, vote_option, early_execution)


@external
@nonreentrant
def infinite_lock_toggle():
    self._access()
    extcall VE.infinite_lock_toggle()


@external
@nonreentrant
def transfer(to: address, amount: uint256):
    assert self.recipient in [msg.sender, to], "Not authorized"
    # If msg.sender is recipient - they can transfer anywhere
    # If msg.sender is NOT recipient - they can transfer only to recipient
    self._cliff()
    extcall YB.transfer(to, amount)


@external
@nonreentrant
def approve(_for: address, amount: uint256):
    self._access()
    self._cliff()
    extcall YB.approve(_for, amount)


@external
@nonreentrant
def recover_token(token: IERC20, to: address, amount: uint256):
    self._access()
    assert token != YB, "Cannot recover YB"
    assert extcall token.transfer(to, amount, default_return_value=True)
    log TokenRecovered(token=token.address, to=to, amount=amount)

