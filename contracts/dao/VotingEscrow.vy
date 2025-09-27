# @version 0.4.3
"""
@title Voting Escrow
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Votes have a weight depending on time, so that users are
        committed to the future of (whatever they are voting for)
@dev Vote weight decays linearly over time. Lock time cannot be
     more than `MAXTIME` (4 years).
"""

# Voting escrow to have time-weighted votes
# Votes have a weight depending on time, so that users are committed
# to the future of (whatever they are voting for).
# The weight in this implementation is linear, and lock cannot be more than maxtime:
# w ^
# 1 +        /
#   |      /
#   |    /
#   |  /
#   |/
# 0 +--------+------> time
#       maxtime (4 years)

from ethereum.ercs import IERC20
from ethereum.ercs import IERC721

from snekmate.auth import ownable
from snekmate.tokens import erc721

from interfaces import IVotes


implements: IVotes

initializes: ownable
initializes: erc721[ownable := ownable]

exports: (
    erc721.balanceOf,
    erc721.ownerOf,
    erc721.approve,
    erc721.setApprovalForAll,
    erc721.getApproved,
    erc721.isApprovedForAll,
    erc721.name,
    erc721.symbol,
    erc721.totalSupply,
    erc721.tokenByIndex,
    erc721.tokenOfOwnerByIndex,
    erc721.tokenURI,
    ownable.transfer_ownership,
    ownable.owner
)


### ve-specific
TOKEN: public(immutable(IERC20))

struct Point:
    bias: int256
    slope: int256  # - dweight / dt
    ts: uint256

struct UntimedPoint:
    bias: uint256
    slope: uint256

struct LockedBalance:
    amount: int256
    end: uint256

flag LockActions:
    DEPOSIT_FOR
    CREATE_LOCK
    INCREASE_AMOUNT
    INCREASE_TIME

interface TransferClearanceChecker:
    def ve_transfer_allowed(user: address) -> bool: view


event Deposit:
    _from: indexed(address)
    _for: indexed(address)
    value: uint256
    locktime: indexed(uint256)
    type: LockActions
    ts: uint256

event Withdraw:
    _from: indexed(address)
    _for: indexed(address)
    value: uint256
    ts: uint256

event Supply:
    prevSupply: uint256
    supply: uint256


event SetTransferClearanceChecker:
    clearance_checker: address


WEEK: constant(uint256) = 7 * 86400  # all future times are rounded by week
MAXTIME: constant(int256) = 4 * 365 * 86400  # 4 years
UMAXTIME: constant(uint256) = 4 * 365 * 86400  # 4 years
WAD: constant(uint256) = 10**18

supply: public(uint256)

locked: public(HashMap[address, LockedBalance])

epoch: public(uint256)
point_history: public(Point[10**18])  # epoch -> unsigned point
user_point_history: public(HashMap[address, Point[10**18]])  # user -> Point[user_epoch]
user_point_epoch: public(HashMap[address, uint256])
slope_changes: public(HashMap[uint256, int256])  # time -> signed slope change

transfer_clearance_checker: public(TransferClearanceChecker)

_SUPPORTED_INTERFACES: constant(bytes4[6]) = [
    0x01FFC9A7, # The ERC-165 identifier for ERC-165.
    0x80AC58CD, # The ERC-165 identifier for ERC-721.
    0x5B5E139F, # The ERC-165 identifier for the ERC-721 metadata extension.
    0x780E9D63, # The ERC-165 identifier for the ERC-721 enumeration extension.
    0x49064906, # The ERC-165 identifier for ERC-4906.
    0xE90FB3F6  # IVotes
]

CLOCK_MODE: public(constant(String[14])) = "mode=timestamp"


@deploy
def __init__(token: IERC20, name: String[25], symbol: String[5], base_uri: String[80]):
    ownable.__init__()
    erc721.__init__(name, symbol, base_uri, "Just say no", "to EIP712")

    TOKEN = token

    self.point_history[0].ts = block.timestamp


@external
@view
def supportsInterface(interface_id: bytes4) -> bool:
    """
    @dev Returns `True` if this contract implements the
         interface defined by `interface_id`.
    @param interface_id The 4-byte interface identifier.
    @return bool The verification whether the contract
            implements the interface or not.
    """
    return interface_id in _SUPPORTED_INTERFACES


@external
@view
def delegates(account: address) -> address:
    """
    @dev Returns the delegate that `account` has chosen (it's a stub because this value cannot be changed)
    """
    return account


@external
def delegate(delegatee: address):
    """
    @dev Delegates votes from the sender to `delegatee`. This reverts because functionality is not supported
    """
    raise "Not supported"


@external
def delegateBySig(delegatee: address, nonce: uint256, expiry: uint256, v: uint8, r: bytes32, s: bytes32):
    """
    @dev Delegates votes from signer to `delegatee`. This reverts because functionality is not supported
    """
    raise "Not supported"


@external
@view
def clock() -> uint48:
    """
    EIP-6372 clock
    """
    return convert(block.timestamp, uint48)


@internal
def _checkpoint(addr: address, old_locked: LockedBalance, new_locked: LockedBalance):
    """
    @notice Record global and per-user data to checkpoint
    @param addr User's wallet address. No user checkpoint if 0x0
    @param old_locked Pevious locked amount / end lock time for the user
    @param new_locked New locked amount / end lock time for the user
    """
    u_old: Point = empty(Point)
    u_new: Point = empty(Point)
    old_dslope: int256 = 0
    new_dslope: int256 = 0
    _epoch: uint256 = self.epoch

    if addr != empty(address):
        # Calculate slopes and biases
        # Kept at zero when they have to
        if old_locked.end > block.timestamp and old_locked.amount > 0:
            if old_locked.end == max_value(uint256):
                u_old.slope = 0
                u_old.bias = old_locked.amount
            else:
                u_old.slope = old_locked.amount // MAXTIME
                u_old.bias = u_old.slope * convert(old_locked.end - block.timestamp, int256)
        if new_locked.end > block.timestamp and new_locked.amount > 0:
            if new_locked.end == max_value(uint256):
                u_new.slope = 0
                u_new.bias = new_locked.amount
            else:
                u_new.slope = new_locked.amount // MAXTIME
                u_new.bias = u_new.slope * convert(new_locked.end - block.timestamp, int256)

        # Read values of scheduled changes in the slope
        # old_locked.end can be in the past and in the future
        # new_locked.end can ONLY by in the FUTURE unless everything expired: than zeros
        old_dslope = self.slope_changes[old_locked.end]
        if new_locked.end != 0:
            if new_locked.end == old_locked.end:
                new_dslope = old_dslope
            else:
                new_dslope = self.slope_changes[new_locked.end]

    last_point: Point = Point(bias=0, slope=0, ts=block.timestamp)
    if _epoch > 0:
        last_point = self.point_history[_epoch]
    last_checkpoint: uint256 = last_point.ts

    # Go over weeks to fill history and calculate what the current point is
    t_i: uint256 = (last_checkpoint // WEEK) * WEEK
    for i: uint256 in range(255):
        # Hopefully it won't happen that this won't get used in 5 years!
        # If it does, users will be able to withdraw but vote weight will be broken
        t_i += WEEK
        d_slope: int256 = 0
        if t_i > block.timestamp:
            t_i = block.timestamp
        else:
            d_slope = self.slope_changes[t_i]
        last_point.bias -= last_point.slope * convert(t_i - last_checkpoint, int256)
        last_point.slope += d_slope
        if last_point.bias < 0:  # This can happen
            last_point.bias = 0
        if last_point.slope < 0:  # This cannot happen - just in case
            last_point.slope = 0
        last_checkpoint = t_i
        last_point.ts = t_i
        _epoch += 1
        if t_i == block.timestamp:
            break
        else:
            self.point_history[_epoch] = last_point

    self.epoch = _epoch
    # Now point_history is filled until t=now

    if addr != empty(address):
        # If last point was in this block, the slope change has been applied already
        # But in such case we have 0 slope(s)
        last_point.slope += (u_new.slope - u_old.slope)
        last_point.bias += (u_new.bias - u_old.bias)
        if last_point.slope < 0:
            last_point.slope = 0
        if last_point.bias < 0:
            last_point.bias = 0

    # Record the changed point into history
    self.point_history[_epoch] = last_point

    if addr != empty(address):
        # Schedule the slope changes (slope is going down)
        # We subtract new_user_slope from [new_locked.end]
        # and add old_user_slope to [old_locked.end]
        if old_locked.end > block.timestamp:
            # old_dslope was <something> - u_old.slope, so we cancel that
            old_dslope += u_old.slope
            if new_locked.end == old_locked.end:
                old_dslope -= u_new.slope  # It was a new deposit, not extension
            self.slope_changes[old_locked.end] = old_dslope

        if new_locked.end > block.timestamp:
            if new_locked.end != old_locked.end:
                new_dslope -= u_new.slope  # old slope disappeared at this point
                self.slope_changes[new_locked.end] = new_dslope
            # else: we recorded it already in old_dslope

        # Now handle user history
        user_epoch: uint256 = self.user_point_epoch[addr] + 1

        self.user_point_epoch[addr] = user_epoch
        u_new.ts = block.timestamp
        self.user_point_history[addr][user_epoch] = u_new


@external
def checkpoint():
    """
    @notice Record global data to checkpoint
    """
    self._checkpoint(empty(address), empty(LockedBalance), empty(LockedBalance))


@internal
def _deposit_for(_addr: address, _value: uint256, unlock_time: uint256, locked_balance: LockedBalance, type: LockActions):
    """
    @notice Deposit and lock tokens for a user
    @param _addr User's wallet address
    @param _value Amount to deposit
    @param unlock_time New time when to unlock the tokens, or 0 if unchanged
    @param locked_balance Previous locked amount / timestamp
    """
    _locked: LockedBalance = locked_balance
    supply_before: uint256 = self.supply

    new_supply: uint256 = (supply_before + _value) // UMAXTIME * UMAXTIME
    rounded_value: uint256 = new_supply - supply_before
    self.supply = new_supply
    old_locked: LockedBalance = _locked
    # Adding to existing lock, or if a lock is expired - creating a new one
    _locked.amount += convert(rounded_value, int256)
    if unlock_time != 0:
        _locked.end = unlock_time
    self.locked[_addr] = _locked

    # Possibilities:
    # Both old_locked.end could be current or expired (>/< block.timestamp)
    # value == 0 (extend lock) or value > 0 (add to lock or extend lock)
    # _locked.end > block.timestamp (always)
    self._checkpoint(_addr, old_locked, _locked)

    if rounded_value != 0:
        assert extcall TOKEN.transferFrom(msg.sender, self, rounded_value)

    log Deposit(_from=msg.sender, _for=_addr, value=rounded_value, locktime=_locked.end, type=type, ts=block.timestamp)
    log Supply(prevSupply=supply_before, supply=new_supply)


@external
@nonreentrant
def create_lock(_value: uint256, _unlock_time: uint256):
    """
    @notice Deposit `_value` tokens for `msg.sender` and lock until `_unlock_time`
    @param _value Amount to deposit
    @param _unlock_time Epoch time when tokens unlock, rounded down to whole weeks
    """
    unlock_time: uint256 = (_unlock_time // WEEK) * WEEK  # Locktime is rounded down to weeks
    _locked: LockedBalance = self.locked[msg.sender]

    assert _value >= UMAXTIME, "Min value"
    assert _locked.amount == 0, "Withdraw old tokens first"
    assert unlock_time > block.timestamp, "Can only lock until time in the future"
    assert unlock_time <= block.timestamp + UMAXTIME, "Voting lock can be 4 years max"

    self._deposit_for(msg.sender, _value, unlock_time, _locked, LockActions.CREATE_LOCK)
    erc721._mint(msg.sender, convert(msg.sender, uint256))


@external
@nonreentrant
def increase_amount(_value: uint256, _for: address = msg.sender):
    """
    @notice Deposit `_value` additional tokens for `_for` which is `msg.sender` by default
            without modifying the unlock time
    @param _value Amount of tokens to deposit and add to the lock
    @param _for Lock to increase for
    """
    _locked: LockedBalance = self.locked[_for]

    assert _value >= UMAXTIME  # dev: need non-zero value
    assert _locked.amount > 0, "No existing lock found"
    assert _locked.end > block.timestamp, "Cannot add to expired lock. Withdraw"

    self._deposit_for(_for, _value, 0, _locked, LockActions.INCREASE_AMOUNT)


@external
@nonreentrant
def increase_unlock_time(_unlock_time: uint256):
    """
    @notice Extend the unlock time for `msg.sender` to `_unlock_time`
    @param _unlock_time New epoch time for unlocking
    """
    _locked: LockedBalance = self.locked[msg.sender]
    unlock_time: uint256 = (_unlock_time // WEEK) * WEEK  # Locktime is rounded down to weeks

    assert _locked.amount > 0, "Nothing is locked"
    assert _locked.end > block.timestamp, "Lock expired"
    assert unlock_time > _locked.end, "Can only increase lock duration"
    assert unlock_time <= block.timestamp + UMAXTIME, "Voting lock can be 4 years max"

    self._deposit_for(msg.sender, 0, unlock_time, _locked, LockActions.INCREASE_TIME)


@external
@nonreentrant
def infinite_lock_toggle():
    """
    @notice Make ever-extending lock or cancel it
    """
    _locked: LockedBalance = self.locked[msg.sender]
    assert _locked.end > block.timestamp, "Lock expired"
    assert _locked.amount > 0, "Nothing is locked"
    unlock_time: uint256 = 0

    if _locked.end == max_value(uint256):
        checker: TransferClearanceChecker = self.transfer_clearance_checker
        if checker.address != empty(address):
            # The check is whether the source (owner) has 0 votes.
            # Destination address can STILL have votes, that's fine
            assert staticcall checker.ve_transfer_allowed(msg.sender), "Not allowed"
        unlock_time = ((block.timestamp + UMAXTIME) // WEEK) * WEEK
    else:
        unlock_time = max_value(uint256)

    self._deposit_for(msg.sender, 0, unlock_time, _locked, LockActions.INCREASE_TIME)


@external
@nonreentrant
def withdraw(_for: address = msg.sender):
    """
    @notice Withdraw all tokens for `msg.sender`
    @dev Only possible if the lock has expired
    """
    _locked: LockedBalance = self.locked[msg.sender]
    assert block.timestamp >= _locked.end, "The lock didn't expire"
    value: uint256 = convert(_locked.amount, uint256)

    old_locked: LockedBalance = _locked
    _locked.end = 0
    _locked.amount = 0
    self.locked[msg.sender] = _locked
    supply_before: uint256 = self.supply
    new_supply: uint256 = supply_before - value
    self.supply = new_supply

    # old_locked can have either expired <= timestamp or zero end
    # _locked has only 0 end
    # Both can have >= 0 amount
    self._checkpoint(msg.sender, old_locked, _locked)

    erc721._burn(convert(msg.sender, uint256))

    assert extcall TOKEN.transfer(_for, value)

    log Withdraw(_from=msg.sender, _for=_for, value=value, ts=block.timestamp)
    log Supply(prevSupply=supply_before, supply=new_supply)


@external
@view
def getVotes(account: address) -> uint256:
    """
    @dev Returns the current amount of votes that `account` has.
    """
    _epoch: uint256 = self.user_point_epoch[account]
    if _epoch == 0:
        return 0
    else:
        last_point: Point = self.user_point_history[account][_epoch]
        last_point.bias -= last_point.slope * convert(block.timestamp - last_point.ts, int256)
        if last_point.bias < 0:
            last_point.bias = 0
        return convert(last_point.bias, uint256)


@external
@view
def getPastVotes(account: address, timepoint: uint256) -> uint256:
    """
    @dev Returns the amount of votes that `account` had at a specific moment in the past
    """
    # Binary search
    _min: uint256 = 0
    _max: uint256 = self.user_point_epoch[account]

    if timepoint < self.user_point_history[account][0].ts:
        return 0

    for i: uint256 in range(128):  # Will be always enough for 128-bit numbers
        if _min >= _max:
            break
        _mid: uint256 = (_min + _max + 1) // 2
        if self.user_point_history[account][_mid].ts <= timepoint:
            _min = _mid
        else:
            _max = _mid - 1

    upoint: Point = self.user_point_history[account][_min]
    upoint.bias -= upoint.slope * convert(timepoint - upoint.ts, int256)

    if upoint.bias >= 0:
        return convert(upoint.bias, uint256)
    else:
        return 0


@internal
@view
def total_supply_at(timepoint: uint256) -> uint256:
    _epoch: uint256 = self.epoch
    if _epoch == 0:
        return 0
    else:
        if timepoint < self.point_history[0].ts:
            return 0

        # Past total supply binary search
        _min: uint256 = 0
        for i: uint256 in range(128):  # Will be always enough for 128-bit numbers
            if _min >= _epoch:
                break
            _mid: uint256 = (_min + _epoch + 1) // 2
            if self.point_history[_mid].ts <= timepoint:
                _min = _mid
            else:
                _epoch = _mid - 1

        point: Point = self.point_history[_min]

        if _min == _epoch:
            # Future total supply search -> iterate over all slope changes
            t_i: uint256 = point.ts  # Already rounded to whole weeks <- NOT really, needs rounding / live with this issue
            # To work around - need to checkpoint before submitting any vote
            for i: uint256 in range(255):
                t_i += WEEK
                d_slope: int256 = 0
                if t_i > timepoint:
                    t_i = timepoint
                else:
                    d_slope = self.slope_changes[t_i]
                point.bias -= point.slope * convert(t_i - point.ts, int256)
                if t_i == timepoint:
                    break
                point.slope += d_slope
                point.ts = t_i

            if point.bias < 0:
                point.bias = 0

        else:
            point.bias -= point.slope * convert(timepoint - point.ts, int256)

        return convert(point.bias, uint256)


@external
@view
def totalVotes() -> uint256:
    """
    @notice Returns current total supply of votes
    """
    return self.total_supply_at(block.timestamp)


@external
@view
def getPastTotalSupply(timepoint: uint256) -> uint256:
    """
    @dev Returns the total supply of votes available at a specific moment in the past.

    @notice This value is the sum of all available votes, which is not necessarily the sum of all delegated votes.
    Votes that have not been delegated are still part of total supply, even though they would not participate in a
    vote.
    Unlike the original method, this one ALSO works with the future
    """
    return self.total_supply_at(timepoint)


@internal
@view
def _ve_transfer_allowed(owner: address, to: address) -> bool:
    checker: TransferClearanceChecker = self.transfer_clearance_checker
    if checker.address != empty(address):
        # The check is whether the source (owner) has 0 votes.
        # Destination address can STILL have votes, that's fine
        assert staticcall checker.ve_transfer_allowed(owner), "Not allowed"
    assert owner != to

    sender_max: bool = False
    receiver_max: bool = False
    max_time: uint256 = (block.timestamp + UMAXTIME) // WEEK * WEEK

    owner_time: uint256 = self.locked[owner].end
    if owner_time == max_value(uint256) or owner_time // WEEK * WEEK == max_time:
        sender_max = True
    to_time: uint256 = self.locked[to].end
    if to_time == max_value(uint256) or to_time // WEEK * WEEK == max_time:
        receiver_max = True

    # the end slope should be the same, that's why the last condition is needed
    return sender_max and receiver_max and (owner_time // WEEK * WEEK == to_time // WEEK * WEEK)


@internal
def _merge_positions(owner: address, to: address):
    """
    @dev Merge veLocked positions of `owner` with `to`, giving it to `to`.
    """
    locked: LockedBalance = self.locked[owner]
    self.locked[owner] = empty(LockedBalance)
    new_locked: LockedBalance = self.locked[to]
    new_locked.amount += locked.amount
    self.locked[to].amount = new_locked.amount

    user_epoch: uint256 = self.user_point_epoch[owner] + 1
    self.user_point_epoch[owner] = user_epoch
    self.user_point_history[owner][user_epoch] = Point(bias=0, slope=0, ts=block.timestamp)

    user_epoch = self.user_point_epoch[to] + 1
    self.user_point_epoch[to] = user_epoch
    slope: int256 = 0
    bias: int256 = 0
    if new_locked.end == max_value(uint256):
        bias = new_locked.amount
    else:
        slope = new_locked.amount // MAXTIME
        bias = slope * convert(new_locked.end - block.timestamp, int256)
    self.user_point_history[to][user_epoch] = Point(
        bias=bias,
        slope=slope,
        ts=block.timestamp
    )

    # Total should not change because we transfer between users

    self._checkpoint(empty(address), empty(LockedBalance), empty(LockedBalance))


@external
def set_transfer_clearance_checker(transfer_clearance_checker: TransferClearanceChecker):
    """
    @notice Set checker for when the transfer os the ve-token is allowed (usually when all votes are removed)
    """
    ownable._check_owner()
    self.transfer_clearance_checker = transfer_clearance_checker
    log SetTransferClearanceChecker(clearance_checker=transfer_clearance_checker.address)


@external
def transferFrom(owner: address, to: address, token_id: uint256):
    """
    @notice Transfer ve-NFT
    """
    assert erc721._is_approved_or_owner(msg.sender, token_id), "erc721: caller is not token owner or approved"
    assert token_id == convert(owner, uint256), "Wrong token ID"
    assert self._ve_transfer_allowed(owner, to), "Need max veLock"
    self._merge_positions(owner, to)
    erc721._burn(token_id)


@external
def safeTransferFrom(owner: address, to: address, token_id: uint256, data: Bytes[1_024] = b""):
    """
    @notice Transfer ve-NFT and use a callback. Keep in mind that NFT gets destructed before the callback is hit
    """
    assert erc721._is_approved_or_owner(msg.sender, token_id), "erc721: caller is not token owner or approved"
    assert token_id == convert(owner, uint256), "Wrong token ID"
    assert self._ve_transfer_allowed(owner, to), "Need max veLock"
    self._merge_positions(owner, to)
    erc721._burn(token_id)
    assert erc721._check_on_erc721_received(owner, to, token_id, data), "erc721: transfer to non-IERC721Receiver implementer"


@external
@view
def get_last_user_slope(addr: address) -> int256:
    """
    @notice Get the most recently recorded rate of voting power decrease for `addr`
    @param addr Address of the user wallet
    @return Value of the slope
    """
    uepoch: uint256 = self.user_point_epoch[addr]
    return self.user_point_history[addr][uepoch].slope


@external
@view
def get_last_user_point(addr: address) -> UntimedPoint:
    """
    @notice Get the most recently recorded point of voting power decrease for `addr`
    @param addr Address of the user wallet
    """
    uepoch: uint256 = self.user_point_epoch[addr]
    return UntimedPoint(
        bias=convert(self.user_point_history[addr][uepoch].bias, uint256),
        slope=convert(self.user_point_history[addr][uepoch].slope, uint256)
    )


@external
@view
def locked__end(_addr: address) -> uint256:
    """
    @notice Get timestamp when `_addr`'s lock finishes
    @param _addr User wallet
    @return Epoch time of the lock end
    """
    return self.locked[_addr].end
