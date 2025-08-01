# @version 0.4.3
"""
@title Gauge Controller
@author Yield Basis
@license MIT
@notice Controls liquidity gauges and the issuance of coins through the gauges
"""
from snekmate.auth import ownable


initializes: ownable


exports: (
    ownable.transfer_ownership,
    ownable.owner
)


# All future times are rounded by week
WEEK: constant(uint256) = 7 * 86400

# Cannot change weight votes more often than once in 10 days
WEIGHT_VOTE_DELAY: constant(uint256) = 10 * 86400


struct Point:
    bias: uint256
    slope: uint256

struct VotedSlope:
    slope: uint256
    bias: uint256  # Only used if slope == 0
    power: uint256
    end: uint256


interface VotingEscrow:
    def get_last_user_slope(addr: address) -> int256: view
    def get_last_user_point(addr: address) -> Point: view
    def locked__end(addr: address) -> uint256: view
    def transfer_clearance_checker() -> address: view

interface Gauge:
    def get_adjustment() -> uint256: view

interface GovernanceToken:
    def emit(owner: address, rate_factor: uint256) -> uint256: nonpayable
    def preview_emissions(t: uint256, rate_factor: uint256) -> uint256: view
    def transfer(_to: address, _amount: uint256) -> bool: nonpayable


event VoteForGauge:
    time: uint256
    user: address
    gauge_addr: address
    weight: uint256

event NewGauge:
    addr: address

event SetKilled:
    gauge: address
    is_killed: bool


TOKEN: public(immutable(GovernanceToken))
VOTING_ESCROW: public(immutable(VotingEscrow))

# Gauge parameters
# All numbers are "fixed point" on the basis of 1e18
n_gauges: public(uint256)

# Needed for enumeration
gauges: public(address[1000000000])
is_killed: public(HashMap[address, bool])

vote_user_slopes: public(HashMap[address, HashMap[address, VotedSlope]])  # user -> gauge_addr -> VotedSlope
vote_user_power: public(HashMap[address, uint256])  # Total vote power used by user
last_user_vote: public(HashMap[address, HashMap[address, uint256]])  # Last user vote's timestamp for each gauge address

# Past and scheduled points for gauge weight, sum of weights per type, total weight
# Point is for bias+slope
# changes_* are for changes in slope
# time_* are for the last change timestamp
# timestamps for changes_ are rounded to whole weeks

# Variables for raw weights of gauges
point_weight: public(HashMap[address, Point])  # gauge_addr -> Point
changes_weight: HashMap[address, HashMap[uint256, uint256]]  # gauge_addr -> weektime -> slope
time_weight: public(HashMap[address, uint256])  # gauge_addr -> last time

gauge_weight: public(HashMap[address, uint256])
gauge_weight_sum: public(uint256)
adjusted_gauge_weight: public(HashMap[address, uint256])
adjusted_gauge_weight_sum: public(uint256)

specific_emissions: public(uint256)
specific_emissions_per_gauge: public(HashMap[address, uint256])
weighted_emissions_per_gauge: public(HashMap[address, uint256])
sent_emissions_per_gauge: public(HashMap[address, uint256])


@deploy
def __init__(token: GovernanceToken, voting_escrow: VotingEscrow):
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


@internal
@view
def _get_weight(gauge: address) -> Point:
    """
    @notice Fill historic gauge weights week-over-week for missed checkins
            and return the total for the future week
    @param gauge Address of the gauge
    @return Gauge weight
    """
    t: uint256 = self.time_weight[gauge]
    current_week: uint256 = block.timestamp // WEEK * WEEK
    dt: uint256 = 0
    if t > 0:
        pt: Point = self.point_weight[gauge]
        for i: uint256 in range(500):
            if t >= current_week:
                dt = block.timestamp - t
                if dt == 0:
                    break
            else:
                dt = (t + WEEK) // WEEK * WEEK - t
            t += dt
            pt.bias -= min(pt.slope * dt, pt.bias)  # Correctly handles even slope=0
            pt.slope -= min(self.changes_weight[gauge][t], pt.slope)  # Value from non-week-boundary is 0
            if pt.bias == 0:
                pt.slope = 0
        return pt
    else:
        return empty(Point)


@internal
def _checkpoint_gauge(gauge: address) -> Point:
    assert self.time_weight[gauge] > 0, "Gauge not alive"

    adjustment: uint256 = min(staticcall Gauge(gauge).get_adjustment(), 10**18)
    t: uint256 = self.time_weight[gauge]

    w: uint256 = self.gauge_weight[gauge]
    aw: uint256 = self.adjusted_gauge_weight[gauge]
    w_sum: uint256 = self.gauge_weight_sum
    aw_sum: uint256 = self.adjusted_gauge_weight_sum

    pt: Point = self._get_weight(gauge)
    self.point_weight[gauge] = pt
    w_new: uint256 = pt.bias
    aw_new: uint256 = w_new * adjustment // 10**18

    self.gauge_weight[gauge] = w_new
    self.gauge_weight_sum = w_sum + w_new - w
    self.adjusted_gauge_weight[gauge] = aw_new
    self.adjusted_gauge_weight_sum = aw_sum + aw_new - aw

    d_emissions: uint256 = extcall TOKEN.emit(self, unsafe_div(aw_sum * 10**18, w_sum))
    self.time_weight[gauge] = block.timestamp

    specific_emissions: uint256 = self.specific_emissions + unsafe_div(d_emissions * 10**18, aw_sum)
    if d_emissions > 0:
        self.specific_emissions = specific_emissions

    if block.timestamp > t:  # Guaranteed to have no new emissions if same time
        self.weighted_emissions_per_gauge[gauge] += (specific_emissions - self.specific_emissions_per_gauge[gauge]) * aw // 10**18
        self.specific_emissions_per_gauge[gauge] = specific_emissions

    return pt


@external
def add_gauge(gauge: address):
    """
    @notice Add gauge `gauge`
    @param gauge Gauge address
    """
    ownable._check_owner()
    assert self.time_weight[gauge] == 0, "Gauge already added"

    n: uint256 = self.n_gauges
    self.n_gauges = n + 1
    self.gauges[n] = gauge
    self.time_weight[gauge] = block.timestamp
    self.specific_emissions_per_gauge[gauge] = self.specific_emissions

    log NewGauge(addr=gauge)


@external
def vote_for_gauge_weights(_gauge_addrs: DynArray[address, 50], _user_weights: DynArray[uint256, 50]):
    """
    @notice Allocate voting power for changing pool weights
    @param _gauge_addrs Gauges which `msg.sender` votes for
    @param _user_weights Weights for a gauge in bps (units of 0.01%). Minimal is 0.01%. Ignored if 0
    """
    # Check if transfer_clearance_checker is set to GC
    assert staticcall VOTING_ESCROW.transfer_clearance_checker() == self, "Vote checker not set"

    n: uint256 = len(_gauge_addrs)
    assert len(_user_weights) == n, "Mismatch in lengths"
    pt: Point = staticcall VOTING_ESCROW.get_last_user_point(msg.sender)
    slope: uint256 = pt.slope
    bias: uint256 = pt.bias  # <- we only use it if locked until max_value(uint256)
    lock_end: uint256 = staticcall VOTING_ESCROW.locked__end(msg.sender)
    assert lock_end > block.timestamp, "Expired"

    power_used: uint256 = self.vote_user_power[msg.sender]

    for i: uint256 in range(50):
        if i >= n:
            break
        _user_weight: uint256 = _user_weights[i]
        _gauge_addr: address = _gauge_addrs[i]
        assert _user_weight <= 10000, "Weight too large"
        if _user_weight != 0:
            assert not self.is_killed[_gauge_addr], "Killed"
        assert self.time_weight[_gauge_addr] > 0, "Gauge not added"
        assert block.timestamp >= self.last_user_vote[msg.sender][_gauge_addr] + WEIGHT_VOTE_DELAY, "Cannot vote so often"

        # Prepare slopes and biases in memory
        old_slope: VotedSlope = self.vote_user_slopes[msg.sender][_gauge_addr]
        old_bias: uint256 = 0
        if old_slope.end == max_value(uint256):
            old_bias = old_slope.bias
        else:
            old_dt: uint256 = max(old_slope.end, block.timestamp) - block.timestamp
            old_bias = old_slope.slope * old_dt
        new_slope: VotedSlope = VotedSlope(
            slope = slope * _user_weight // 10000,
            bias = 0,
            power = _user_weight,
            end = lock_end
        )
        new_bias: uint256 = 0
        if lock_end == max_value(uint256):
            new_bias = bias * _user_weight // 10000
        else:
            new_bias = new_slope.slope * (lock_end - block.timestamp)  # dev: raises when expired
        new_slope.bias = new_bias

        power_used = power_used + new_slope.power - old_slope.power

        pt = self._checkpoint_gauge(_gauge_addr)  # Contains old_weight_bias and old_weight_slope

        ## Remove old and schedule new slope changes
        # Remove slope changes for old slopes
        # Schedule recording of initial slope for next_time
        self.point_weight[_gauge_addr].bias = max(pt.bias + new_bias, old_bias) - old_bias
        if old_slope.end > block.timestamp:
            self.point_weight[_gauge_addr].slope = max(pt.slope + new_slope.slope, old_slope.slope) - old_slope.slope
        else:
            self.point_weight[_gauge_addr].slope += new_slope.slope
        if old_slope.end > block.timestamp:
            # Cancel old slope changes if they still didn't happen
            self.changes_weight[_gauge_addr][old_slope.end] -= old_slope.slope
        # Add slope changes for new slopes
        self.changes_weight[_gauge_addr][new_slope.end] += new_slope.slope

        self.vote_user_slopes[msg.sender][_gauge_addr] = new_slope

        # point_weight has changed, so we are doing another checkpoint to enact the vote
        self._checkpoint_gauge(_gauge_addr)

        # Record last action time
        self.last_user_vote[msg.sender][_gauge_addr] = block.timestamp

        log VoteForGauge(time=block.timestamp, user=msg.sender, gauge_addr=_gauge_addr, weight=_user_weight)

    # Check and update powers (weights) used
    assert power_used <= 10000, 'Used too much power'
    self.vote_user_power[msg.sender] = power_used


@external
@view
def get_gauge_weight(addr: address) -> uint256:
    """
    @notice Get current gauge weight
    @param addr Gauge address
    @return Gauge weight
    """
    return self._get_weight(addr).bias


@external
@view
def gauge_relative_weight(gauge: address) -> uint256:
    """
    @notice Get Gauge relative weight (not more than 1.0) normalized to 1e18
            (e.g. 1.0 == 1e18). Inflation which will be received by it is
            inflation_rate * relative_weight / 1e18
    @param gauge Gauge address
    @return Value of relative weight normalized to 1e18
    """
    return unsafe_div(self.adjusted_gauge_weight[gauge] * 10**18, self.adjusted_gauge_weight_sum)


@external
@view
def ve_transfer_allowed(user: address) -> bool:
    return self.vote_user_power[user] == 0


@external
def checkpoint(gauge: address):
    """
    @notice Checkpoint a gauge
    """
    self._checkpoint_gauge(gauge)


@external
@view
def preview_emissions(gauge: address, at_time: uint256) -> uint256:
    """
    @notice Checkpoint logic is re-done here without causing writes
    """
    if self.time_weight[gauge] == 0:
        return 0

    w: uint256 = self.gauge_weight[gauge]
    aw: uint256 = self.adjusted_gauge_weight[gauge]
    w_sum: uint256 = self.gauge_weight_sum
    aw_sum: uint256 = self.adjusted_gauge_weight_sum

    d_emissions: uint256 = 0
    if at_time > self.time_weight[gauge]:
        d_emissions = staticcall TOKEN.preview_emissions(at_time, unsafe_div(aw_sum * 10**18, w_sum))
    specific_emissions: uint256 = self.specific_emissions + unsafe_div(d_emissions * 10**18, aw_sum)
    weighted_emissions_per_gauge: uint256 = self.weighted_emissions_per_gauge[gauge] + (specific_emissions - self.specific_emissions_per_gauge[gauge]) * aw // 10**18

    return weighted_emissions_per_gauge - self.sent_emissions_per_gauge[gauge]


@external
def emit() -> uint256:
    self._checkpoint_gauge(msg.sender)
    emissions: uint256 = self.weighted_emissions_per_gauge[msg.sender]
    to_send: uint256 = emissions - self.sent_emissions_per_gauge[msg.sender]
    self.sent_emissions_per_gauge[msg.sender] = emissions
    if to_send > 0:
        extcall TOKEN.transfer(msg.sender, to_send)
    return to_send


@external
def set_killed(gauge: address, is_killed: bool):
    ownable._check_owner()
    assert self.time_weight[gauge] > 0, "Gauge not added"
    self.is_killed[gauge] = is_killed
    log SetKilled(gauge=gauge, is_killed=is_killed)
