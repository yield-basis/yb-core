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
    """
    @param token Token to be distributed by the CliffEscrow
    @param ve VotingEscrow (ve-locker)
    @param gc GaugeController
    """
    YB = token
    VE = ve
    GC = gc
    self.recipient = self


@external
def initialize(recipient: address, unlock_time: uint256) -> bool:
    """
    @notice Initialize an instance created by a factory contract
    @param recipient Recipient of the tokens (one per contract!)
    @param unlock_time When all the tokens can be released (cliff time)
    """
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
    """
    @notice Create a ve-lock while affected by the cliff
    @param _value Amount to ve-lock
    @param _unlock_time Time for ve-lock to end
    """
    self._access()
    extcall VE.create_lock(_value, _unlock_time)


@external
@nonreentrant
def increase_amount(_value: uint256):
    """
    @notice Increase amount in the ve-lock
    @param _value Number of tokens to add to ve-lock
    """
    self._access()
    extcall VE.increase_amount(_value)


@external
@nonreentrant
def increase_unlock_time(_unlock_time: uint256):
    """
    @notice Increase the duration of ve-lock
    @param _unlock_time New unlock timestamp (seconds)
    """
    self._access()
    extcall VE.increase_unlock_time(_unlock_time)


@external
@nonreentrant
def withdraw():
    """
    @notice Withdraw all tokens from expired ve-lock back to the CliffEscrow contract
    """
    self._access()
    extcall VE.withdraw()


@external
@nonreentrant
def transferFrom(owner: address, to: address, token_id: uint256):
    """
    @notice Transfer ve-locked NFT which the CliffEscrow has access to anywhere - only after cliff is finished
    """
    self._access()
    self._cliff()
    extcall VE.transferFrom(owner, to, token_id)


@external
@nonreentrant
def vote_for_gauge_weights(_gauge_addrs: DynArray[address, 50], _user_weights: DynArray[uint256, 50]):
    """
    @notice Vote for gauge weights from inside the CliffEscrow with a ve-lock we created
    @param _gauge_addrs Gauges to vote for
    @param _user_weights Voting weights of the gauges
    """
    self._access()
    extcall GC.vote_for_gauge_weights(_gauge_addrs, _user_weights)


@external
@nonreentrant
def aragon_vote(dao: AragonDAO, proposal_id: uint256, vote_option: uint8, early_execution: bool):
    """
    @notice Perform an Aragon vote using ve-lock we have inside CliffEscrow
    @param dao Aragon DAO voting plugin address
    @param proposal_id Proposal to vote for
    @param vote_option Option to choose when voting
    @param early_execution Early execution parameter
    """
    self._access()
    extcall dao.vote(proposal_id, vote_option, early_execution)


@external
@nonreentrant
def infinite_lock_toggle():
    """
    @notice Make ve-lock automatically relocking or remove this setting
    """
    self._access()
    extcall VE.infinite_lock_toggle()


@external
@nonreentrant
def transfer(to: address, amount: uint256):
    """
    @notice Transfer the token (not ve-lock!) anywhere. Requires cliff to be finished.
            Recipient of CliffEscrow can transfer anywhere, but everyone else only to recipient.
    """
    assert self.recipient in [msg.sender, to], "Not authorized"
    # If msg.sender is recipient - they can transfer anywhere
    # If msg.sender is NOT recipient - they can transfer only to recipient
    self._cliff()
    extcall YB.transfer(to, amount)


@external
@nonreentrant
def approve(_for: address, amount: uint256):
    """
    @notice Approve the cliff-affected tokens for transfering out. Only after cliff is finished.
    @param _for Address which can take our tokens
    @param amount Amount approved
    """
    self._access()
    self._cliff()
    extcall YB.approve(_for, amount)


@external
@nonreentrant
def recover_token(token: IERC20, to: address, amount: uint256):
    """
    @notice Recover (send) any token not affected by cliff
    @param token Token to recover
    @param to Address to send to
    @param amount Amount of token to send
    """
    self._access()
    assert token != YB, "Cannot recover YB"
    assert extcall token.transfer(to, amount, default_return_value=True)
    log TokenRecovered(token=token.address, to=to, amount=amount)
