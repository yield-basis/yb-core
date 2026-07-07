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


# The real FeeDistributor (contracts/dao/FeeDistributor.vy) stores the sets as a
# DynArray[IERC20, MAX_TOKENS][N], so its only public accessor is the element getter
# token_sets(set_id, i) -> token (no whole-array getter, no length). We enumerate it.
interface FeeDistributor:
    def current_token_set() -> uint256: view
    def token_sets(set_id: uint256, i: uint256) -> IERC20: view
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
    """
    @notice Deploy the splitter.
    @param fee_distributor The veYB FeeDistributor that receives the non-PID share
                           and whose token set defines the LT tokens to split.
    @param pid The PID controller that receives the split-off reserve share.
    @param split_fraction Fraction (1e18) of each LT balance routed to the PID.
    @param owner DAO address that owns the configuration.
    """
    ownable.__init__()
    ownable._transfer_ownership(owner)
    assert split_fraction <= PRECISION, "fraction > 1"
    self.fee_distributor = fee_distributor
    self.pid = pid
    self.split_fraction = split_fraction


@internal
@view
def _token_set(fd: FeeDistributor) -> DynArray[IERC20, MAX_TOKENS]:
    """Read the FeeDistributor's current token set. Its `token_sets` is a
    DynArray[IERC20, MAX_TOKENS][N], so the only getter is token_sets(set_id, i) -> token,
    with no whole-array getter and no length - enumerate by index until it reverts."""
    set_id: uint256 = staticcall fd.current_token_set()
    out: DynArray[IERC20, MAX_TOKENS] = []
    for i: uint256 in range(MAX_TOKENS):
        success: bool = False
        response: Bytes[32] = b""
        success, response = raw_call(
            fd.address,
            abi_encode(set_id, i, method_id=method_id("token_sets(uint256,uint256)")),
            max_outsize=32, is_static_call=True, revert_on_failure=False)
        if not success:
            break  # index past the end of the DynArray -> bounds check reverted
        out.append(IERC20(abi_decode(response, address)))
    return out


@external
@nonreentrant
def trigger():
    """
    @notice Realize LT admin fees, split them PID/FeeDistributor, then poke both.
            Permissionless.
    """
    fd: FeeDistributor = self.fee_distributor
    token_set: DynArray[IERC20, MAX_TOKENS] = self._token_set(fd)
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


@internal
def _check_fee_distributor(fd: FeeDistributor):
    """
    @notice Sanity-check that `fd` really is a FeeDistributor.
    @dev fill_epochs() is permissionless and a no-op when no new tokens arrived, so
         calling it here just reverts if the target doesn't implement the interface.
    @param fd Candidate FeeDistributor to validate.
    """
    extcall fd.fill_epochs()


@external
def set_split_fraction(fraction: uint256):
    """
    @notice Set the fraction (1e18) of incoming LT fees routed to the PID.
    @dev DAO only. Must be <= 1e18.
    @param fraction New split fraction, 1e18 == 100%.
    """
    ownable._check_owner()
    assert fraction <= PRECISION, "fraction > 1"
    self.split_fraction = fraction
    log SetSplitFraction(fraction=fraction)


@external
def set_destinations(pid: address, fee_distributor: FeeDistributor):
    """
    @notice Set the PID reserve and FeeDistributor destinations.
    @dev DAO only. Sanity-checks the FeeDistributor by exercising fill_epochs().
    @param pid New PID controller receiving the split-off share.
    @param fee_distributor New FeeDistributor receiving the remainder.
    """
    ownable._check_owner()
    self._check_fee_distributor(fee_distributor)
    self.pid = pid
    self.fee_distributor = fee_distributor
    log SetDestinations(pid=pid, fee_distributor=fee_distributor.address)


@external
def recover(token: IERC20, amount: uint256, to: address):
    """
    @notice Sweep any tokens held by this contract out to `to`.
    @dev DAO only.
    @param token Token to sweep.
    @param amount Amount to transfer.
    @param to Recipient.
    """
    ownable._check_owner()
    assert extcall token.transfer(to, amount, default_return_value=True)
    log Recover(token=token.address, amount=amount)
