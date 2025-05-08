# @version 0.4.1
"""
@title Liquidity Gauge
@author Yield Basis
@license MIT
"""
from snekmate.auth import ownable
from snekmate.tokens import erc20


initializes: ownable
initializes: erc20[ownable := ownable]


exports: (
    erc20.IERC20,
    erc20.decimals
)


interface GaugeController:
    def is_killed(gauge: address) -> bool: view
    def emit() -> uint256: nonpayable
    def TOKEN() -> erc20.IERC20: view

interface Factory:
    def GAUGE_CONTROLLER() -> GaugeController: view

interface IERC20Slice:
    def symbol() -> String[29]: view


struct Reward:
    token: address
    distributor: address
    period_finish: uint256
    rate: uint256
    last_update: uint256
    integral: uint256


VERSION: public(constant(String[8])) = "v1.0.0"

MAX_REWARDS: constant(uint256) = 8
GC: public(immutable(GaugeController))
YB: public(immutable(erc20.IERC20))
LP_TOKEN: public(immutable(erc20.IERC20))

# For tracking external rewards
reward_count: public(uint256)
reward_tokens: public(address[MAX_REWARDS])

reward_data: public(HashMap[address, Reward])

# claimant -> default reward receiver
rewards_receiver: public(HashMap[address, address])

# reward token -> claiming address -> integral
reward_integral_for: public(HashMap[address, HashMap[address, uint256]])

# user -> [256 claimable amount][uint256 claimed amount]
claim_data: HashMap[address, HashMap[address, uint256]]

# 1e18 * ∫(rate(t) / totalSupply(t) dt) from (last_action) till checkpoint
integrate_inv_supply_of: public(HashMap[address, uint256])
integrate_checkpoint_of: public(HashMap[address, uint256])

# ∫(balance * rate(t) / totalSupply(t) dt) from 0 till checkpoint
# Units: rate * t = already number of coins per address to issue
integrate_fraction: public(HashMap[address, uint256])

inflation_rate: public(uint256)

# The goal is to be able to calculate ∫(rate * balance / totalSupply dt) from 0 till checkpoint
# All values are kept in units of being multiplied by 1e18
period: public(uint256)
period_timestamp: public(HashMap[uint256, uint256])

# 1e18 * ∫(rate(t) / totalSupply(t) dt) from 0 till checkpoint
integrate_inv_supply: public(HashMap[uint256, uint256])


@deploy
def __init__(lp_token: erc20.IERC20):
    ownable.__init__()
    erc20.__init__("YB Gauge: ..", "g(..)", 18, "Just say no", "to EIP712")
    LP_TOKEN = lp_token
    GC = staticcall Factory(msg.sender).GAUGE_CONTROLLER()
    YB = staticcall GC.TOKEN()


@external
@view
def symbol() -> String[32]:
    return concat('g(', staticcall IERC20Slice(LP_TOKEN.address).symbol(), ')')


@external
@view
def name() -> String[39]:
    return concat('YB Gauge: ', staticcall IERC20Slice(LP_TOKEN.address).symbol())
