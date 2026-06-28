# @version 0.4.3
"""
@title FeeSplitter
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Sits in front of the FeeDistributor as the Factory fee_receiver. Splits a
        DAO-set fraction of incoming LT fees to the PID (net-pressure reserve) and
        forwards the rest to the FeeDistributor, then pokes both.
@dev The set of LT tokens is read from the FeeDistributor (single source of truth),
     not duplicated here.
"""
from ethereum.ercs import IERC20
from snekmate.auth import ownable


initializes: ownable
exports: (ownable.owner, ownable.transfer_ownership)


interface FeeDistributor:
    def current_token_set() -> uint256: view
    def token_sets(i: uint256) -> DynArray[IERC20, MAX_TOKENS]: view
    def fill_epochs(): nonpayable

interface PID:
    def trigger(): nonpayable


event SetSplitFraction:
    fraction: uint256

event SetDestinations:
    pid: address
    fee_distributor: address

event Recover:
    token: indexed(address)
    amount: uint256


PRECISION: constant(uint256) = 10**18
MAX_TOKENS: constant(uint256) = 100   # must match FeeDistributor.MAX_TOKENS

split_fraction: public(uint256)       # 1e18, portion routed to the PID
pid: public(address)
fee_distributor: public(FeeDistributor)


@deploy
def __init__(fee_distributor: FeeDistributor, pid: address, split_fraction: uint256, owner: address):
    ownable.__init__()
    ownable._transfer_ownership(owner)
    assert split_fraction <= PRECISION, "fraction > 1"
    self.fee_distributor = fee_distributor
    self.pid = pid
    self.split_fraction = split_fraction


@external
@nonreentrant
def trigger():
    """
    @notice Realize LT admin fees, split them PID/FeeDistributor, then poke both.
            Permissionless.
    """
    fd: FeeDistributor = self.fee_distributor
    token_set: DynArray[IERC20, MAX_TOKENS] = staticcall fd.token_sets(staticcall fd.current_token_set())
    pid: address = self.pid
    fraction: uint256 = self.split_fraction

    for lt: IERC20 in token_set:
        # Best-effort: realize fresh admin fees (mints LT shares to this contract).
        # An LT with nothing to withdraw simply leaves the balance unchanged.
        realized: bool = raw_call(lt.address, method_id("withdraw_admin_fees()"),
                                  max_outsize=0, revert_on_failure=False)

        bal: uint256 = staticcall lt.balanceOf(self)
        if bal == 0:
            continue
        to_pid: uint256 = bal * fraction // PRECISION
        if to_pid > 0:
            assert extcall lt.transfer(pid, to_pid, default_return_value=True)
        rest: uint256 = bal - to_pid
        if rest > 0:
            assert extcall lt.transfer(fd.address, rest, default_return_value=True)

    if pid != empty(address):
        extcall PID(pid).trigger()
    extcall fd.fill_epochs()


@external
def set_split_fraction(fraction: uint256):
    ownable._check_owner()
    assert fraction <= PRECISION, "fraction > 1"
    self.split_fraction = fraction
    log SetSplitFraction(fraction=fraction)


@external
def set_destinations(pid: address, fee_distributor: FeeDistributor):
    ownable._check_owner()
    self.pid = pid
    self.fee_distributor = fee_distributor
    log SetDestinations(pid=pid, fee_distributor=fee_distributor.address)


@external
def recover(token: IERC20, amount: uint256, to: address):
    """@notice DAO sweep of any tokens held here."""
    ownable._check_owner()
    assert extcall token.transfer(to, amount, default_return_value=True)
    log Recover(token=token.address, amount=amount)
