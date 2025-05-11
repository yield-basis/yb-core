# @version 0.4.1
"""
@title Liquidity Gauge
@author Yield Basis
@license MIT
"""
from ethereum.ercs import IERC20
from snekmate.extensions import erc4626


initializes: erc4626


exports: (
    erc4626.IERC20,
    erc4626.IERC4626,
    erc4626.decimals,
    erc4626.ownable.transfer_ownership,
    erc4626.ownable.owner
)


interface GaugeController:
    def is_killed(gauge: address) -> bool: view
    def emit() -> uint256: nonpayable
    def preview_emissions(gauge: address, at_time: uint256) -> uint256: view
    def TOKEN() -> IERC20: view

interface Factory:
    def GAUGE_CONTROLLER() -> GaugeController: view
    def admin() -> address: view

interface IERC20Slice:
    def symbol() -> String[29]: view


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
YB: public(immutable(IERC20))
LP_TOKEN: public(immutable(IERC20))


reward_count: public(uint256)
reward_tokens: public(IERC20[MAX_REWARDS])
rewards: public(HashMap[IERC20, Reward])

integral_inv_supply: public(Integral)
integral_inv_supply_4_token: public(HashMap[IERC20, uint256])

reward_rate_integral: public(HashMap[IERC20, Integral])
reward_rate_integral_4_user: public(HashMap[address, HashMap[IERC20, uint256]])

user_rewards_integral: public(HashMap[address, HashMap[IERC20, Integral]])


@deploy
def __init__(lp_token: IERC20):
    erc4626.__init__("YB Gauge: ..", "g(..)", lp_token, 0, "Just say no", "to EIP712")
    LP_TOKEN = lp_token
    GC = staticcall Factory(msg.sender).GAUGE_CONTROLLER()
    YB = staticcall GC.TOKEN()
    erc4626.ownable.owner = staticcall Factory(msg.sender).admin()
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
def _checkpoint(reward: IERC20, d_reward: uint256, user: address) -> RewardIntegrals:
    r: RewardIntegrals = empty(RewardIntegrals)

    r.integral_inv_supply = self.integral_inv_supply
    if block.timestamp > r.integral_inv_supply.t:
        r.integral_inv_supply.v += 10**36 * (block.timestamp - r.integral_inv_supply.t) // erc4626.erc20.totalSupply
        r.integral_inv_supply.t = block.timestamp

    if reward.address != empty(address):
        r.reward_rate_integral = self.reward_rate_integral[reward]
        if block.timestamp > r.reward_rate_integral.t:
            r.reward_rate_integral.v += (r.integral_inv_supply.v - self.integral_inv_supply_4_token[reward]) * d_reward //\
               (block.timestamp - r.reward_rate_integral.t)
            r.reward_rate_integral.t = block.timestamp

    if user != empty(address):
        r.user_rewards_integral = self.user_rewards_integral[user][reward]
        if block.timestamp > r.user_rewards_integral.t:
            r.d_user_reward = (r.reward_rate_integral.v - self.reward_rate_integral_4_user[user][reward]) *\
                erc4626.erc20.balanceOf[user] // 10**18
            r.user_rewards_integral.v += r.d_user_reward
            r.user_rewards_integral.t = block.timestamp

    return r


@internal
@view
def _get_vested_rewards(token: IERC20) -> uint256:
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


@internal
def _vest_rewards(reward: IERC20) -> uint256:
    d_reward: uint256 = 0
    if reward == YB:
        d_reward = extcall GC.emit()
    else:
        d_reward = self._get_vested_rewards(reward)
    return d_reward


@external
@nonreentrant
def claim(reward: IERC20 = YB, user: address = msg.sender) -> uint256:
    d_reward: uint256 = self._vest_rewards(reward)
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
def preview_claim(reward: IERC20, user: address) -> uint256:
    d_reward: uint256 = 0
    if reward == YB:
        d_reward = staticcall GC.preview_emissions(self, block.timestamp)
    else:
        d_reward = self._get_vested_rewards(reward)
    return self._checkpoint(reward, d_reward, user).d_user_reward


@external
def add_reward(token: IERC20, distributor: address):
    assert token != YB, "YB"
    assert distributor != empty(address)
    assert self.rewards[token].distributor == empty(address), "Already added"
    erc4626.ownable._check_owner()
    self.rewards[token].distributor = distributor
    reward_id: uint256 = self.reward_count
    self.reward_tokens[reward_id] = token
    self.reward_count = reward_id + 1
    log AddReward(token=token.address, distributor=distributor, id=reward_id)


@external
def change_reward_distributor(token: IERC20, distributor: address):
    assert token != YB, "YB"
    assert distributor != empty(address)
    assert self.rewards[token].distributor != empty(address), "Not added"
    erc4626.ownable._check_owner()
    self.rewards[token].distributor = distributor
    log ChangeRewardDistributor(token=token.address, distributor=distributor)


@external
def deposit_reward(token: IERC20, amount: uint256, finish_time: uint256):
    assert token != YB, "YB"
    assert amount > 0, "No rewards"
    r: Reward = self.rewards[token]

    if msg.sender != r.distributor:
        erc4626.ownable._check_owner()

    d_reward: uint256 = self._vest_rewards(token)
    ri: RewardIntegrals = self._checkpoint(token, d_reward, empty(address))
    self.integral_inv_supply = ri.integral_inv_supply
    self.integral_inv_supply_4_token[token] = ri.integral_inv_supply.v
    self.reward_rate_integral[token] = ri.reward_rate_integral

    used_rewards: uint256 = ri.reward_rate_integral.v

    if finish_time > 0:
        # Change rate to meet new finish time
        assert finish_time > block.timestamp, "Finishes in the past"
        r.finish_time = finish_time
    else:
        # Keep the reward rate
        assert r.finish_time > block.timestamp, "Rate unknown"
        r.finish_time = block.timestamp + (r.finish_time - block.timestamp) * (r.total + amount) // r.total
    r.total += amount

    assert extcall token.transferFrom(msg.sender, self, amount, default_return_value=True)
    self.rewards[token] = r
    log DepositRewards(token=token.address, distributor=msg.sender, amount=amount, finish_time=r.finish_time)


# XXX checkpoint at transfers, desposits and withdrawals, adding rewards
