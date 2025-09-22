# @version 0.4.3
"""
@title Liquidity Gauge
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Liquidity Gauge to measure who deposited LP tokens for how long
"""
from ethereum.ercs import IERC20
from snekmate.utils import math
import erc4626


initializes: erc4626


exports: (
    erc4626.erc20.totalSupply,
    erc4626.erc20.balanceOf,
    erc4626.erc20.approve,
    erc4626.erc20.allowance,
    erc4626.decimals,
    erc4626.totalAssets,
    erc4626.convertToShares,
    erc4626.convertToAssets,
    erc4626.maxDeposit,
    erc4626.previewDeposit,
    erc4626.maxMint,
    erc4626.previewMint,
    erc4626.maxWithdraw,
    erc4626.previewWithdraw,
    erc4626.maxRedeem,
    erc4626.previewRedeem,
    erc4626.asset,
    erc4626.ownable.transfer_ownership,
    erc4626.ownable.owner,
    erc4626.MIN_SHARES
)


interface GaugeController:
    def emit() -> uint256: nonpayable
    def preview_emissions(gauge: address, at_time: uint256) -> uint256: view
    def TOKEN() -> IERC20: view

interface Factory:
    def gauge_controller() -> GaugeController: view
    def admin() -> address: view
    def emergency_admin() -> address: view

interface IERC20Slice:
    def symbol() -> String[29]: view

interface LT:
    def is_killed() -> bool: view
    def checkpoint_staker_rebase(): nonpayable


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


VERSION: public(constant(String[8])) = "v1.0.0"

MAX_REWARDS: constant(uint256) = 8
MIN_SHARES_DECIMALS: constant(uint8) = 12
GC: public(immutable(GaugeController))
YB: public(immutable(IERC20))
LP_TOKEN: public(immutable(IERC20))
FACTORY: public(immutable(Factory))


reward_count: public(uint256)
reward_tokens: public(IERC20[MAX_REWARDS])
rewards: public(HashMap[IERC20, Reward])
processed_rewards: public(HashMap[IERC20, uint256])

integral_inv_supply: public(Integral)
integral_inv_supply_4_token: public(HashMap[IERC20, uint256])

reward_rate_integral: public(HashMap[IERC20, Integral])
reward_rate_integral_4_user: public(HashMap[address, HashMap[IERC20, uint256]])

user_rewards_integral: public(HashMap[address, HashMap[IERC20, Integral]])
claimed_rewards: public(HashMap[address, HashMap[IERC20, uint256]])


@deploy
def __init__(lp_token: IERC20):
    erc4626.__init__("YB Gauge: ..", "g(..)", lp_token, MIN_SHARES_DECIMALS, "Just say no", "to EIP712")
    LP_TOKEN = lp_token
    GC = staticcall Factory(msg.sender).gauge_controller()
    YB = staticcall GC.TOKEN()
    FACTORY = Factory(msg.sender)
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


@external
@view
def get_adjustment() -> uint256:
    """
    @notice Get a measure of how many Liquidity Tokens are staked: sqrt(staked / totalSupply)
    @return Result from 0.0 (0) to 1.0 (1e18)
    """
    staked: uint256 = staticcall LP_TOKEN.balanceOf(self)
    supply: uint256 = staticcall LP_TOKEN.totalSupply()
    return isqrt(unsafe_div(staked * 10**36, supply))


@internal
@view
def _checkpoint(reward: IERC20, d_reward: uint256, user: address) -> RewardIntegrals:
    r: RewardIntegrals = empty(RewardIntegrals)

    r.integral_inv_supply = self.integral_inv_supply
    if block.timestamp > r.integral_inv_supply.t:
        r.integral_inv_supply.v += unsafe_div(10**36 * (block.timestamp - r.integral_inv_supply.t), erc4626.erc20.totalSupply)
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
            r.user_rewards_integral.v += math._mul_div(
                r.reward_rate_integral.v - self.reward_rate_integral_4_user[user][reward],
                erc4626.erc20.balanceOf[user],
                10**36,
                False)
            r.user_rewards_integral.t = block.timestamp

    return r


@internal
@view
def _get_vested_rewards(token: IERC20) -> uint256:
    assert self.rewards[token].distributor != empty(address), "No reward"

    last_reward_time: uint256 = self.reward_rate_integral[token].t
    used_rewards: uint256 = self.processed_rewards[token]
    finish_time: uint256 = self.rewards[token].finish_time
    total: uint256 = self.rewards[token].total
    if finish_time > last_reward_time:
        new_used: uint256 = (total - used_rewards) * (block.timestamp - last_reward_time) //\
            (finish_time - last_reward_time) + used_rewards
        return min(new_used, total) - used_rewards
    else:
        return 0


@internal
def _vest_rewards(reward: IERC20, pre: bool) -> uint256:
    d_reward: uint256 = 0
    if reward == YB:
        if pre:
            d_reward = staticcall GC.preview_emissions(self, block.timestamp)
        else:
            d_reward = extcall GC.emit()
    else:
        d_reward = self._get_vested_rewards(reward)
        self.processed_rewards[reward] += d_reward
    return d_reward


@internal
def _checkpoint_user(user: address):
    n: uint256 = self.reward_count
    for i: uint256 in range(MAX_REWARDS):
        if i == n:
            break
        reward: IERC20 = self.reward_tokens[i]
        d_reward: uint256 = self._vest_rewards(reward, True)
        r: RewardIntegrals = self._checkpoint(reward, d_reward, user)
        if i == 0:
            self.integral_inv_supply = r.integral_inv_supply
        self.integral_inv_supply_4_token[reward] = r.integral_inv_supply.v
        self.reward_rate_integral[reward] = r.reward_rate_integral
        self.reward_rate_integral_4_user[user][reward] = r.reward_rate_integral.v
        self.user_rewards_integral[user][reward] = r.user_rewards_integral


@external
@nonreentrant
def claim(reward: IERC20 = YB, user: address = msg.sender) -> uint256:
    """
    @notice Claim rewards (YB or external) earned by the user
    @param reward Reward token (YB by default)
    @param user User to claim for
    """
    d_reward: uint256 = self._vest_rewards(reward, False)
    r: RewardIntegrals = self._checkpoint(reward, d_reward, user)

    self.integral_inv_supply = r.integral_inv_supply
    self.integral_inv_supply_4_token[reward] = r.integral_inv_supply.v
    self.reward_rate_integral[reward] = r.reward_rate_integral
    self.reward_rate_integral_4_user[user][reward] = r.reward_rate_integral.v
    self.user_rewards_integral[user][reward] = r.user_rewards_integral

    d_reward = r.user_rewards_integral.v - self.claimed_rewards[user][reward]
    self.claimed_rewards[user][reward] = r.user_rewards_integral.v

    assert extcall reward.transfer(user, d_reward, default_return_value=True)
    return d_reward


@external
@view
def preview_claim(reward: IERC20, user: address) -> uint256:
    """
    @notice Calculate amount of rewards which user can claim
    @param reward Reward token address
    @param user Recipient address
    """
    d_reward: uint256 = 0
    if reward == YB:
        d_reward = staticcall GC.preview_emissions(self, block.timestamp)
    else:
        d_reward = self._get_vested_rewards(reward)

    r: RewardIntegrals = self._checkpoint(reward, d_reward, user)
    return r.user_rewards_integral.v - self.claimed_rewards[user][reward]


@external
@nonreentrant
def add_reward(token: IERC20, distributor: address):
    """
    @notice Add a non-YB reward token. This does not deposit it, just creates a possibility to do it
    @param token Token address to add as an extra reward
    @param distributor Address of distributor of the reward
    """
    assert token != YB, "YB"
    assert token != LP_TOKEN, "LP_TOKEN"
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
    """
    @notice Change the distributor of a custom (non-YB) reward
    @param token Reward token address
    @param distributor New distributor of the reward
    """
    assert token != YB, "YB"
    assert distributor != empty(address)
    assert self.rewards[token].distributor != empty(address), "Not added"
    erc4626.ownable._check_owner()
    self.rewards[token].distributor = distributor
    log ChangeRewardDistributor(token=token.address, distributor=distributor)


@external
@nonreentrant
def deposit_reward(token: IERC20, amount: uint256, finish_time: uint256):
    """
    @notice Deposit a custom (non-YB) reward token if it was added with add_reward
    @param token Reward token address
    @param amount Amount of token to deposit
    @param finish_time Timestamp when distribution should finish. Do not change the reward rate if set to 0
    """
    assert token != YB, "YB"
    assert amount > 0, "No rewards"
    r: Reward = self.rewards[token]

    if msg.sender != r.distributor:
        erc4626.ownable._check_owner()

    d_reward: uint256 = self._vest_rewards(token, False)
    ri: RewardIntegrals = self._checkpoint(token, d_reward, empty(address))
    self.integral_inv_supply = ri.integral_inv_supply
    self.integral_inv_supply_4_token[token] = ri.integral_inv_supply.v
    self.reward_rate_integral[token] = ri.reward_rate_integral

    unused_rewards: uint256 = r.total - self.processed_rewards[token]

    if finish_time > 0 or unused_rewards == 0:
        # Change rate to meet new finish time
        assert finish_time > block.timestamp, "Finishes in the past"
        r.finish_time = finish_time
    else:
        # Keep the reward rate
        assert r.finish_time > block.timestamp, "Rate unknown"
        r.finish_time = block.timestamp + (r.finish_time - block.timestamp) * (unused_rewards + amount) // unused_rewards
    r.total += amount

    self.rewards[token] = r
    assert extcall token.transferFrom(msg.sender, self, amount, default_return_value=True)
    log DepositRewards(token=token.address, distributor=msg.sender, amount=amount, finish_time=r.finish_time)


@external
@nonreentrant
def deposit(assets: uint256, receiver: address) -> uint256:
    """
    @notice Deposit liquidity token to earn rewards
    @param assets Amount of LT token to deposit
    @param receiver Who should get the gauge tokens
    """
    extcall LT(LP_TOKEN.address).checkpoint_staker_rebase()
    assert assets <= erc4626._max_deposit(receiver), "erc4626: deposit more than maximum"
    shares: uint256 = erc4626._preview_deposit(assets)
    self._checkpoint_user(receiver)
    erc4626._deposit(msg.sender, receiver, assets, shares)
    erc4626._check_min_shares()
    extcall GC.emit()
    return shares


@external
@nonreentrant
def mint(shares: uint256, receiver: address) -> uint256:
    """
    @notice Deposit liquidity token to earn rewards given amount of gauge shares to receive
    @param shares Gauge shares to receive
    @param receiver Receiver of the gauge shares
    """
    extcall LT(LP_TOKEN.address).checkpoint_staker_rebase()
    assert shares <= erc4626._max_mint(receiver), "erc4626: mint more than maximum"
    assets: uint256 = erc4626._preview_mint(shares)
    self._checkpoint_user(receiver)
    erc4626._deposit(msg.sender, receiver, assets, shares)
    erc4626._check_min_shares()
    extcall GC.emit()
    return assets


@external
@nonreentrant
def withdraw(assets: uint256, receiver: address, owner: address) -> uint256:
    """
    @notice Withdraw gauge shares given the amount of LT tokens to receive
    @param assets Amount of LT tokens to receive
    @param receiver Receiver of LT tokens
    @param owner Who had the gauge tokens before the tx
    """
    extcall LT(LP_TOKEN.address).checkpoint_staker_rebase()
    assert assets <= erc4626._max_withdraw(owner), "erc4626: withdraw more than maximum"
    shares: uint256 = erc4626._preview_withdraw(assets)
    self._checkpoint_user(owner)
    erc4626._withdraw(msg.sender, receiver, owner, assets, shares)
    erc4626._check_min_shares()
    extcall GC.emit()
    return shares


@external
@nonreentrant
def redeem(shares: uint256, receiver: address, owner: address) -> uint256:
    """
    @notice Withdraw gauge shares given the amount of gauge shares
    @param shares Amount of gauge shares to withdraw
    @param receiver Receiver of LT tokens
    @param owner Who had the gauge tokens before the tx
    """
    extcall LT(LP_TOKEN.address).checkpoint_staker_rebase()
    assert shares <= erc4626._max_redeem(owner), "erc4626: redeem more than maximum"

    # Handle killing so that eadmin can withdraw anyone's shares to their own wallet
    sender: address = msg.sender
    if staticcall LT(LP_TOKEN.address).is_killed():
        if msg.sender == staticcall FACTORY.emergency_admin():
            # Only emergency admin is allowed to withdraw for others, but then only transfer to themselves
            assert receiver == owner, "receiver"
            sender = owner  # Tell _withdraw() to bypass checks who can do it

    assets: uint256 = erc4626._preview_redeem(shares)
    self._checkpoint_user(owner)
    erc4626._withdraw(sender, receiver, owner, assets, shares)
    erc4626._check_min_shares()
    extcall GC.emit()
    return assets


@external
@nonreentrant
def transfer(to: address, amount: uint256) -> bool:
    """
    @notice ERC20 transfer of gauge shares
    """
    self._checkpoint_user(msg.sender)
    self._checkpoint_user(to)
    erc4626.erc20._transfer(msg.sender, to, amount)
    extcall GC.emit()
    return True


@external
@nonreentrant
def transferFrom(owner: address, to: address, amount: uint256) -> bool:
    """
    @notice ERC20 transferFrom of gauge shares
    """
    self._checkpoint_user(owner)
    self._checkpoint_user(to)
    erc4626.erc20._spend_allowance(owner, msg.sender, amount)
    erc4626.erc20._transfer(owner, to, amount)
    extcall GC.emit()
    return True
