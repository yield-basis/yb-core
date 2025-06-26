# @version 0.4.1
"""
@title Vesting Escrow
@author Yield Basis
@license MIT
@notice Vests `ERC20` tokens for multiple addresses over multiple vesting periods
"""
from ethereum.ercs import IERC20
from snekmate.auth import ownable


initializes: ownable


exports: (
    ownable.renounce_ownership,
    ownable.transfer_ownership,
    ownable.owner
)


event Fund:
    recipient: indexed(address)
    amount: uint256

event Claim:
    recipient: indexed(address)
    claimed: uint256

event ToggleDisable:
    recipient: address
    disabled: bool


TOKEN: public(immutable(IERC20))
START_TIME: public(immutable(uint256))
END_TIME: public(immutable(uint256))
initial_locked: public(HashMap[address, uint256])
total_claimed: public(HashMap[address, uint256])

initial_locked_supply: public(uint256)
unallocated_supply: public(uint256)

can_disable: public(bool)
disabled_at: public(HashMap[address, uint256])


@deploy
def __init__(
    _token: IERC20,
    _start_time: uint256,
    _end_time: uint256,
    _can_disable: bool
):
    """
    @param _token Address of the ERC20 token being distributed
    @param _start_time Timestamp at which the distribution starts. Should be in
        the future, so that we have enough time to VoteLock everyone
    @param _end_time Time until everything should be vested
    @param _can_disable Whether admin can disable accounts in this deployment
    """
    ownable.__init__()

    assert _start_time >= block.timestamp
    assert _end_time > _start_time

    TOKEN = _token
    START_TIME = _start_time
    END_TIME = _end_time
    self.can_disable = _can_disable


@external
def add_tokens(_amount: uint256):
    """
    @notice Transfer vestable tokens into the contract
    @dev Handled separate from `fund` to reduce transaction count when using funding admins
    @param _amount Number of tokens to transfer
    """
    ownable._check_owner()
    self.unallocated_supply += _amount
    assert extcall TOKEN.transferFrom(msg.sender, self, _amount, default_return_value=True)


@external
def fund(_recipients: DynArray[address, 100], _amounts: DynArray[uint256, 100]):
    """
    @notice Vest tokens for multiple recipients
    @param _recipients List of addresses to fund
    @param _amounts Amount of vested tokens for each address
    """
    ownable._check_owner()

    _total_amount: uint256 = 0
    for i: uint256 in range(100):
        if i == len(_recipients):
            break
        amount: uint256 = _amounts[i]
        recipient: address = _recipients[i]
        _total_amount += amount
        self.initial_locked[recipient] += amount
        log Fund(recipient=recipient, amount=amount)

    self.initial_locked_supply += _total_amount
    self.unallocated_supply -= _total_amount


@external
def toggle_disable(_recipient: address):
    """
    @notice Disable or re-enable a vested address's ability to claim tokens
    @dev When disabled, the address is only unable to claim tokens which are still
         locked at the time of this call. It is not possible to block the claim
         of tokens which have already vested.
    @param _recipient Address to disable or enable
    """
    ownable._check_owner()
    assert self.can_disable, "Cannot disable"

    is_disabled: bool = self.disabled_at[_recipient] == 0
    if is_disabled:
        self.disabled_at[_recipient] = block.timestamp
    else:
        self.disabled_at[_recipient] = 0

    log ToggleDisable(recipient=_recipient, disabled=is_disabled)


@external
def disable_can_disable():
    """
    @notice Disable the ability to call `toggle_disable`
    """
    ownable._check_owner()
    self.can_disable = False


@internal
@view
def _total_vested_of(_recipient: address, _time: uint256 = block.timestamp) -> uint256:
    locked: uint256 = self.initial_locked[_recipient]
    if _time < START_TIME:
        return 0
    return min(locked * (_time - START_TIME) // (END_TIME - START_TIME), locked)


@internal
@view
def _total_vested() -> uint256:
    locked: uint256 = self.initial_locked_supply
    if block.timestamp < START_TIME:
        return 0
    return min(locked * (block.timestamp - START_TIME) // (END_TIME - START_TIME), locked)


@external
@view
def vestedSupply() -> uint256:
    """
    @notice Get the total number of tokens which have vested, that are held
            by this contract
    """
    return self._total_vested()


@external
@view
def lockedSupply() -> uint256:
    """
    @notice Get the total number of tokens which are still locked
            (have not yet vested)
    """
    return self.initial_locked_supply - self._total_vested()


@external
@view
def vestedOf(_recipient: address) -> uint256:
    """
    @notice Get the number of tokens which have vested for a given address
    @param _recipient address to check
    """
    return self._total_vested_of(_recipient)


@external
@view
def balanceOf(_recipient: address) -> uint256:
    """
    @notice Get the number of unclaimed, vested tokens for a given address
    @param _recipient address to check
    """
    return self._total_vested_of(_recipient) - self.total_claimed[_recipient]


@external
@view
def lockedOf(_recipient: address) -> uint256:
    """
    @notice Get the number of locked tokens for a given address
    @param _recipient address to check
    """
    return self.initial_locked[_recipient] - self._total_vested_of(_recipient)


@external
def claim(addr: address = msg.sender):
    """
    @notice Claim tokens which have vested
    @param addr Address to claim tokens for
    """
    t: uint256 = self.disabled_at[addr]
    if t == 0:
        t = block.timestamp
    claimable: uint256 = self._total_vested_of(addr, t) - self.total_claimed[addr]
    self.total_claimed[addr] += claimable
    assert extcall TOKEN.transfer(addr, claimable, default_return_value=True)

    log Claim(recipient=addr, claimed=claimable)
