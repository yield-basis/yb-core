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
    erc20.decimals,
    ownable.transfer_ownership,
    ownable.owner
)


interface GaugeController:
    def is_killed(gauge: address) -> bool: view
    def emit() -> uint256: nonpayable
    def preview_emissions(gauge: address, at_time: uint256) -> uint256: view
    def TOKEN() -> erc20.IERC20: view

interface Factory:
    def GAUGE_CONTROLLER() -> GaugeController: view
    def admin() -> address: view

interface IERC20Slice:
    def symbol() -> String[29]: view


event Deposit:
    provider: indexed(address)
    value: uint256

event Withdraw:
    provider: indexed(address)
    value: uint256

event AddReward:
    token: indexed(address)
    distributor: address
    id: uint256

event ChangeRewardDistributor:
    token: indexed(address)
    distributor: address

event DepositRewards:
    token: indexed(address)
    distributor: address
    amount: uint256
    finish_time: uint256


struct Reward:
    distributor: address
    finish_time: uint256
    total: uint256

struct Integral:
    v: uint256
    t: uint256

struct RewardIntegrals:
    integral_inv_supply: Integral
    reward_rate_integral: Integral
    user_rewards_integral: Integral
    d_user_reward: uint256


VERSION: public(constant(String[8])) = "v1.0.0"

MAX_REWARDS: constant(uint256) = 8
GC: public(immutable(GaugeController))
YB: public(immutable(erc20.IERC20))
LP_TOKEN: public(immutable(erc20.IERC20))


reward_count: public(uint256)
reward_tokens: public(HashMap[uint256, erc20.IERC20])
rewards: public(HashMap[erc20.IERC20, Reward])

integral_inv_supply: public(Integral)
integral_inv_supply_4_token: public(HashMap[erc20.IERC20, uint256])

reward_rate_integral: public(HashMap[erc20.IERC20, Integral])
reward_rate_integral_4_user: public(HashMap[address, HashMap[erc20.IERC20, uint256]])

user_rewards_integral: public(HashMap[address, HashMap[erc20.IERC20, Integral]])


@deploy
def __init__(lp_token: erc20.IERC20):
    ownable.__init__()
    erc20.__init__("YB Gauge: ..", "g(..)", 18, "Just say no", "to EIP712")
    LP_TOKEN = lp_token
    GC = staticcall Factory(msg.sender).GAUGE_CONTROLLER()
    YB = staticcall GC.TOKEN()
    ownable.owner = staticcall Factory(msg.sender).admin()
    self.rewards[YB].distributor = GC.address
    self.reward_tokens[0] = YB
    self.reward_count = 1
    log AddReward(token=YB.address, distributor=GC.address, id=0)


@external
@view
def symbol() -> String[32]:
    return concat('g(', staticcall IERC20Slice(LP_TOKEN.address).symbol(), ')')


@external
@view
def name() -> String[39]:
    return concat('YB Gauge: ', staticcall IERC20Slice(LP_TOKEN.address).symbol())


@internal
@view
def _checkpoint(reward: erc20.IERC20, d_reward: uint256, user: address) -> RewardIntegrals:
    r: RewardIntegrals = empty(RewardIntegrals)

    r.integral_inv_supply = self.integral_inv_supply
    r.integral_inv_supply.v += 10**36 * (block.timestamp - r.integral_inv_supply.t) // erc20.totalSupply
    r.integral_inv_supply.t = block.timestamp

    r.reward_rate_integral = self.reward_rate_integral[reward]
    if block.timestamp > r.reward_rate_integral.t:
        r.reward_rate_integral.v += (r.integral_inv_supply.v - self.integral_inv_supply_4_token[reward]) * d_reward //\
           (block.timestamp - r.reward_rate_integral.t)
        r.reward_rate_integral.t = block.timestamp

    r.user_rewards_integral = self.user_rewards_integral[user][reward]
    if block.timestamp > r.user_rewards_integral.t:
        r.d_user_reward = (r.reward_rate_integral.v - self.reward_rate_integral_4_user[user][reward]) *\
            erc20.balanceOf[user] // 10**18
        r.user_rewards_integral.v += r.d_user_reward
        r.user_rewards_integral.t = block.timestamp

    return r


@internal
@view
def _get_vested_rewards(token: erc20.IERC20) -> uint256:
    assert self.rewards[token].distributor != empty(address), "No reward"

    last_reward_time: uint256 = self.reward_rate_integral[token].t
    used_rewards: uint256 = self.reward_rate_integral[token].v
    finish_time: uint256 = self.rewards[token].finish_time
    total: uint256 = self.rewards[token].total
    if finish_time > last_reward_time:
        new_used: uint256 = (total - used_rewards) * (block.timestamp - last_reward_time) //\
            (finish_time - last_reward_time) + used_rewards
        return min(new_used, total) - used_rewards
    else:
        return 0


@external
@nonreentrant
def claim(reward: erc20.IERC20 = YB, user: address = msg.sender) -> uint256:
    d_reward: uint256 = 0
    if reward == YB:
        d_reward = extcall GC.emit()
    else:
        d_reward = self._get_vested_rewards(reward)

    r: RewardIntegrals = self._checkpoint(reward, d_reward, user)

    self.integral_inv_supply = r.integral_inv_supply
    self.integral_inv_supply_4_token[reward] = r.integral_inv_supply.v
    self.reward_rate_integral[reward] = r.reward_rate_integral
    self.reward_rate_integral_4_user[user][reward] = r.reward_rate_integral.v
    self.user_rewards_integral[user][reward] = r.user_rewards_integral

    assert extcall reward.transfer(user, r.d_user_reward, default_return_value=True)
    return r.d_user_reward


@external
@view
def preview_claim(reward: erc20.IERC20, user: address) -> uint256:
    d_reward: uint256 = 0
    if reward == YB:
        d_reward = staticcall GC.preview_emissions(self, block.timestamp)
    else:
        d_reward = self._get_vested_rewards(reward)
    return self._checkpoint(reward, d_reward, user).d_user_reward


@external
def add_reward(token: erc20.IERC20, distributor: address):
    assert token != YB, "YB"
    assert distributor != empty(address)
    assert self.rewards[token].distributor == empty(address), "Already added"
    ownable._check_owner()
    self.rewards[token].distributor = distributor
    reward_id: uint256 = self.reward_count
    self.reward_tokens[reward_id] = token
    self.reward_count = reward_id + 1
    log AddReward(token=token.address, distributor=distributor, id=reward_id)


@external
def change_reward_distributor(token: erc20.IERC20, distributor: address):
    assert token != YB, "YB"
    assert distributor != empty(address)
    assert self.rewards[token].distributor != empty(address), "Not added"
    ownable._check_owner()
    self.rewards[token].distributor = distributor
    log ChangeRewardDistributor(token=token.address, distributor=distributor)


@external
def deposit_reward(token: erc20.IERC20, amount: uint256, finish_time: uint256):
    assert token != YB, "YB"
    assert amount > 0, "No rewards"
    r: Reward = self.rewards[token]

    if msg.sender != r.distributor:
        ownable._check_owner()

    last_reward_time: uint256 = self.reward_rate_integral[token].t
    used_rewards: uint256 = self.reward_rate_integral[token].v

    if finish_time > 0:
        # Change rate to meet new finish time
        assert finish_time > block.timestamp, "Finishes in the past"
        r.finish_time = finish_time
    else:
        # Keep the reward rate
        assert r.finish_time > last_reward_time, "Rate unknown"
        r.finish_time = last_reward_time + (r.finish_time - last_reward_time) * (r.total + amount) // r.total
    r.total += amount

    assert extcall token.transferFrom(msg.sender, self, amount, default_return_value=True)
    self.rewards[token] = r
    log DepositRewards(token=token.address, distributor=msg.sender, amount=amount, finish_time=r.finish_time)


# deposit
# withdraw
