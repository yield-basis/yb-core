# @version 0.4.1
"""
@title Gauge Controller
@author Yield Basis
@license MIT
@notice Controls liquidity gauges and the issuance of coins through the gauges
"""
from ethereum.ercs import IERC20
from snekmate.auth import ownable


initializes: ownable


exports: (
    ownable.transfer_ownership
)


# All future times are rounded by week
WEEK: constant(uint256) = 7 * 86400
IWEEK: constant(int256) = 7 * 86400

# Cannot change weight votes more often than once in 10 days
WEIGHT_VOTE_DELAY: constant(uint256) = 10 * 86400


struct Point:
    bias: int256
    slope: int256

struct VotedSlope:
    slope: int256
    power: int256
    end: int256


interface VotingEscrow:
    def get_last_user_slope(addr: address) -> int256: view
    def locked__end(addr: address) -> uint256: view


event NewGaugeWeight:
    gauge_address: address
    time: uint256
    weight: uint256
    total_weight: uint256

event VoteForGauge:
    time: uint256
    user: address
    gauge_addr: address
    weight: uint256

event NewGauge:
    addr: address


admin: public(address)  # Can and will be a smart contract

TOKEN: public(immutable(IERC20))
VOTING_ESCROW: public(immutable(VotingEscrow))

# Gauge parameters
# All numbers are "fixed point" on the basis of 1e18
n_gauges: public(uint256)

# Needed for enumeration
gauges: public(address[1000000000])

vote_user_slopes: public(HashMap[address, HashMap[address, VotedSlope]])  # user -> gauge_addr -> VotedSlope
vote_user_power: public(HashMap[address, uint256])  # Total vote power used by user
last_user_vote: public(HashMap[address, HashMap[address, uint256]])  # Last user vote's timestamp for each gauge address

# Past and scheduled points for gauge weight, sum of weights per type, total weight
# Point is for bias+slope
# changes_* are for changes in slope
# time_* are for the last change timestamp
# timestamps are rounded to whole weeks

points_weight: public(HashMap[address, HashMap[uint256, Point]])  # gauge_addr -> time -> Point
changes_weight: HashMap[address, HashMap[uint256, int256]]  # gauge_addr -> time -> slope
time_weight: public(HashMap[address, uint256])  # gauge_addr -> last scheduled time (next week)

points_sum: public(HashMap[uint256, Point])  # time -> Point
changes_sum: HashMap[uint256, int256]  # time -> slope
time_sum: public(uint256)  # last scheduled time


@deploy
def __init__(token: IERC20, voting_escrow: VotingEscrow):
    """
    @notice Contract constructor
    @param token `ERC20CRV` contract address
    @param voting_escrow `VotingEscrow` contract address
    """
    ownable.__init__()

    assert token.address != empty(address)
    assert voting_escrow.address != empty(address)

    TOKEN = token
    VOTING_ESCROW = voting_escrow
    self.time_sum = block.timestamp // WEEK * WEEK


@internal
def _get_sum() -> int256:
    """
    @notice Fill historic total weights week-over-week for missed checkins
            and return the total for the future week
    @return Total weight
    """
    t: uint256 = self.time_sum

    if t > 0:
        pt: Point = self.points_sum[t]
        for i: uint256 in range(500):
            if t > block.timestamp:
                break
            t += WEEK
            pt.bias -= pt.slope * IWEEK
            pt.slope -= self.changes_sum[t]
            if pt.bias <= 0:
                pt.bias = 0
                pt.slope = 0
            self.points_sum[t] = pt
            if t > block.timestamp:
                self.time_sum = t
        return pt.bias

    else:
        return 0


@internal
def _get_weight(gauge_addr: address) -> int256:
    """
    @notice Fill historic gauge weights week-over-week for missed checkins
            and return the total for the future week
    @param gauge_addr Address of the gauge
    @return Gauge weight
    """
    t: uint256 = self.time_weight[gauge_addr]
    if t > 0:
        pt: Point = self.points_weight[gauge_addr][t]
        for i: uint256 in range(500):
            if t > block.timestamp:
                break
            t += WEEK
            pt.bias -= pt.slope * IWEEK
            pt.slope -= self.changes_weight[gauge_addr][t]
            if pt.bias <= 0:
                pt.bias = 0
                pt.slope = 0
            self.points_weight[gauge_addr][t] = pt
            if t > block.timestamp:
                self.time_weight[gauge_addr] = t
        return pt.bias
    else:
        return 0


@external
@view
def get_gauge_weight(addr: address) -> uint256:
    """
    @notice Get current gauge weight
    @param addr Gauge address
    @return Gauge weight
    """
    return convert(self.points_weight[addr][self.time_weight[addr]].bias, uint256)


@external
@view
def get_total_weight() -> uint256:
    """
    @notice Get current total (type-weighted) weight
    @return Total weight
    """
    return convert(self.points_sum[self.time_sum].bias, uint256)


@internal
@view
def _gauge_relative_weight(addr: address, time: uint256) -> uint256:
    """
    @notice Get Gauge relative weight (not more than 1.0) normalized to 1e18
            (e.g. 1.0 == 1e18). Inflation which will be received by it is
            inflation_rate * relative_weight / 1e18
    @param addr Gauge address
    @param time Relative weight at the specified timestamp in the past or present
    @return Value of relative weight normalized to 1e18
    """
    t: uint256 = time // WEEK * WEEK
    _total_weight: uint256 = convert(self.points_sum[t].bias, uint256)

    if _total_weight > 0:
        _gauge_weight: uint256 = convert(self.points_weight[addr][t].bias, uint256)
        return 10**18 * _gauge_weight // _total_weight

    else:
        return 0


@external
def checkpoint():
    """
    @notice Checkpoint to fill data common for all gauges
    """
    self._get_sum()


@external
def checkpoint_gauge(addr: address):
    """
    @notice Checkpoint to fill data for both a specific gauge and common for all gauges
    @param addr Gauge address
    """
    self._get_weight(addr)
    self._get_sum()


@external
def add_gauge(addr: address):
    """
    @notice Add gauge `addr` of type `gauge_type` with weight `weight`
    @param addr Gauge address
    """
    ownable._check_owner()

    n: uint256 = self.n_gauges
    self.n_gauges = n + 1
    self.gauges[n] = addr

    self.time_weight[addr] = (block.timestamp + WEEK) // WEEK * WEEK

    log NewGauge(addr=addr)


@external
@view
def gauge_relative_weight(addr: address, time: uint256 = block.timestamp) -> uint256:
    """
    @notice Get Gauge relative weight (not more than 1.0) normalized to 1e18
            (e.g. 1.0 == 1e18). Inflation which will be received by it is
            inflation_rate * relative_weight / 1e18
    @param addr Gauge address
    @param time Relative weight at the specified timestamp in the past or present
    @return Value of relative weight normalized to 1e18
    """
    return self._gauge_relative_weight(addr, time)


@external
def gauge_relative_weight_write(addr: address, time: uint256 = block.timestamp) -> uint256:
    """
    @notice Get gauge weight normalized to 1e18 and also fill all the unfilled
            values for type and gauge records
    @dev Any address can call, however nothing is recorded if the values are filled already
    @param addr Gauge address
    @param time Relative weight at the specified timestamp in the past or present
    @return Value of relative weight normalized to 1e18
    """
    self._get_weight(addr)
    self._get_sum()  # Also calculates get_sum
    return self._gauge_relative_weight(addr, time)
