# @version 0.4.1
"""
@title Gauge Controller
@author Yield Basis
@license MIT
@notice Controls liquidity gauges and the issuance of coins through the gauges
"""
from ethereum.ercs import IERC20

# All future times are rounded by week
WEEK: constant(uint256) = 7 * 86400

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
    weight: uint256


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
changes_weight: HashMap[address, HashMap[uint256, uint256]]  # gauge_addr -> time -> slope
time_weight: public(HashMap[address, uint256])  # gauge_addr -> last scheduled time (next week)

points_sum: public(HashMap[uint256, Point])  # time -> Point
changes_sum: HashMap[uint256, uint256]  # time -> slope

points_total: public(HashMap[uint256, uint256])  # time -> total weight
time_total: public(uint256)  # last scheduled time


@deploy
def __init__(token: IERC20, voting_escrow: VotingEscrow):
    """
    @notice Contract constructor
    @param token `ERC20CRV` contract address
    @param voting_escrow `VotingEscrow` contract address
    """
    assert token.address != empty(address)
    assert voting_escrow.address != empty(address)

    self.admin = msg.sender
    TOKEN = token
    VOTING_ESCROW = voting_escrow
    self.time_total = block.timestamp // WEEK * WEEK
