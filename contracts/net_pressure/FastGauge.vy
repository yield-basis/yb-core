# @version 0.4.3
"""
@title FastGauge
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice ERC4626 staking gauge for a Curve stableswap LP token that streams a
        single reward (crvUSD) at a rate the PID controller can change quickly.
@dev Like a Curve LiquidityGauge extra-reward (no boost), but instead of funding
     a fixed amount over a week, the PID sets the per-second `reward_rate` and the
     gauge PULLS crvUSD from the PID at every checkpoint. If the PID reserve runs
     dry, the pull is capped to what is available, so the effective rate simply
     drops to zero with no reverts. Reward accounting mirrors Curve V5:
         reward_integral += pulled * 1e18 / totalSupply
         claimable[user] += balanceOf[user] * (reward_integral - integral_for[user]) / 1e18
"""
from ethereum.ercs import IERC20
from snekmate.utils import math
from ..dao import erc4626


initializes: erc4626


exports: (
    erc4626.erc20.totalSupply,
    erc4626.erc20.balanceOf,
    erc4626.erc20.approve,
    erc4626.erc20.allowance,
    erc4626.decimals,
    erc4626.name,
    erc4626.symbol,
    erc4626.asset,
    erc4626.totalAssets,
    erc4626.convertToShares,
    erc4626.convertToAssets,
    erc4626.previewDeposit,
    erc4626.previewMint,
    erc4626.previewWithdraw,
    erc4626.previewRedeem,
    erc4626.maxDeposit,
    erc4626.maxMint,
    erc4626.maxWithdraw,
    erc4626.maxRedeem,
    erc4626.ownable.transfer_ownership,
    erc4626.ownable.owner,
)


event SetPID:
    pid: address

event SetRewardRate:
    rate: uint256

event Claim:
    user: indexed(address)
    amount: uint256


PRECISION: constant(uint256) = 10**18
# Seed-the-market floor: total supply must be 0 or >= this (shares are 1:1 with the
# staked LP). This blocks the ERC4626 first-depositor / donation inflation attack
# with a meaningful ~$10 seed instead of relying on tiny dead-share dust: to grief a
# victim depositing V the attacker must donate > V * MIN_TOTAL_SUPPLY, and can never
# bootstrap a 1-share vault. The last withdrawal must also exit fully (to 0) or leave
# >= MIN_TOTAL_SUPPLY.
MIN_TOTAL_SUPPLY: public(constant(uint256)) = 10 * 10**18
REWARD_TOKEN: public(immutable(IERC20))  # crvUSD
LP_TOKEN: public(immutable(IERC20))      # Curve stableswap LP staked here

# The PID controller: the only address allowed to set the rate and the source the
# gauge pulls reward tokens from. Set by the DAO (owner) after deployment.
pid: public(address)

reward_rate: public(uint256)             # crvUSD per second, set by the PID
reward_integral: public(uint256)         # accumulated reward per share, 1e18
last_update: public(uint256)
reward_integral_for: public(HashMap[address, uint256])
claimable: public(HashMap[address, uint256])  # settled, unclaimed crvUSD per user


@deploy
def __init__(lp_token: IERC20, reward_token: IERC20, owner: address):
    """
    @notice Deploy a gauge staking `lp_token` and streaming `reward_token`.
    @param lp_token The Curve stableswap LP token staked here (the vault asset).
    @param reward_token The streamed reward token (crvUSD).
    @param owner DAO address that can set the PID.
    """
    # Pass 0 so the module's own MIN_SHARES check is a no-op (MIN_SHARES = 1); we
    # enforce our own MIN_TOTAL_SUPPLY floor below instead.
    erc4626.__init__("YB FastGauge", "fg", lp_token, 0, "Just say no", "to EIP712")
    erc4626.ownable.owner = owner
    LP_TOKEN = lp_token
    REWARD_TOKEN = reward_token
    self.last_update = block.timestamp


@internal
@view
def _check_min_supply():
    """
    @notice Enforce the seed-the-market floor: total supply is 0 or >= MIN_TOTAL_SUPPLY.
    """
    supply: uint256 = erc4626.erc20.totalSupply
    assert supply == 0 or supply >= MIN_TOTAL_SUPPLY, "Below min supply"


# --- reward accounting -------------------------------------------------------

@internal
@view
def _available_from_pid() -> uint256:
    """
    @notice crvUSD the gauge can pull from the PID right now (caps the stream).
    @return min(PID balance, PID->gauge allowance), or 0 if no PID is set.
    """
    pid: address = self.pid
    if pid == empty(address):
        return 0
    return min(
        staticcall REWARD_TOKEN.balanceOf(pid),
        staticcall REWARD_TOKEN.allowance(pid, self),
    )


@internal
def _checkpoint(user: address):
    """
    @notice Settle the global reward integral (pulling crvUSD from the PID) and the
            user's claimable balance.
    @dev Must run BEFORE any change to balances/totalSupply. Pass empty(address) to
         settle only the global integral.
    @param user User whose claimable balance to settle (empty to skip).
    """
    integral: uint256 = self.reward_integral
    supply: uint256 = erc4626.erc20.totalSupply

    if block.timestamp > self.last_update:
        if supply > 0 and self.reward_rate > 0:
            owed: uint256 = self.reward_rate * (block.timestamp - self.last_update)
            pulled: uint256 = min(owed, self._available_from_pid())
            if pulled > 0:
                assert extcall REWARD_TOKEN.transferFrom(self.pid, self, pulled, default_return_value=True)
                integral += pulled * PRECISION // supply
                self.reward_integral = integral
        self.last_update = block.timestamp

    if user != empty(address):
        integral_for: uint256 = self.reward_integral_for[user]
        if integral > integral_for:
            self.claimable[user] += erc4626.erc20.balanceOf[user] * (integral - integral_for) // PRECISION
            self.reward_integral_for[user] = integral


@external
@view
def claimable_reward(user: address) -> uint256:
    """
    @notice crvUSD currently claimable by `user`.
    @dev Projected to now and clamped by what the PID could actually supply, so it
         matches what claim() would pay.
    @param user Account to query.
    @return Claimable crvUSD amount.
    """
    integral: uint256 = self.reward_integral
    supply: uint256 = erc4626.erc20.totalSupply
    if block.timestamp > self.last_update and supply > 0 and self.reward_rate > 0:
        owed: uint256 = self.reward_rate * (block.timestamp - self.last_update)
        pulled: uint256 = min(owed, self._available_from_pid())
        integral += pulled * PRECISION // supply
    return self.claimable[user] + erc4626.erc20.balanceOf[user] * (integral - self.reward_integral_for[user]) // PRECISION


@external
@nonreentrant
def claim(user: address = msg.sender) -> uint256:
    """
    @notice Claim crvUSD rewards earned by `user`, paid from the gauge's balance.
    @param user Account to claim for (rewards are sent to this address).
    @return Amount of crvUSD paid out.
    """
    self._checkpoint(user)
    amount: uint256 = self.claimable[user]
    if amount > 0:
        self.claimable[user] = 0
        assert extcall REWARD_TOKEN.transfer(user, amount, default_return_value=True)
    log Claim(user=user, amount=amount)
    return amount


# --- PID / DAO control -------------------------------------------------------

@external
def set_reward_rate(rate: uint256):
    """
    @notice Set the crvUSD/second stream rate. Callable only by the PID.
    @dev Settles the global integral at the old rate first.
    @param rate New stream rate in crvUSD per second.
    """
    assert msg.sender == self.pid, "Only PID"
    self._checkpoint(empty(address))
    self.reward_rate = rate
    log SetRewardRate(rate=rate)


@external
def set_pid(pid: address):
    """
    @notice Set the PID controller (reward source and rate setter). DAO only.
    @param pid New PID controller address.
    """
    erc4626.ownable._check_owner()
    self._checkpoint(empty(address))
    self.pid = pid
    log SetPID(pid=pid)


# --- ERC4626 entrypoints (checkpoint before every balance change) ------------

@external
@nonreentrant
def deposit(assets: uint256, receiver: address) -> uint256:
    """
    @notice Stake `assets` LP tokens, minting gauge shares to `receiver`.
    @dev Checkpoints rewards before the balance change.
    @param assets Amount of LP tokens to stake.
    @param receiver Recipient of the minted gauge shares.
    @return Gauge shares minted.
    """
    assert assets <= erc4626._max_deposit(receiver), "erc4626: deposit more than maximum"
    shares: uint256 = erc4626._preview_deposit(assets)
    self._checkpoint(receiver)
    erc4626._deposit(msg.sender, receiver, assets, shares)
    self._check_min_supply()
    return shares


@external
@nonreentrant
def mint(shares: uint256, receiver: address) -> uint256:
    """
    @notice Mint `shares` gauge tokens to `receiver` by staking the required LP.
    @dev Checkpoints rewards before the balance change.
    @param shares Amount of gauge shares to mint.
    @param receiver Recipient of the minted gauge shares.
    @return LP tokens pulled from the caller.
    """
    assert shares <= erc4626._max_mint(receiver), "erc4626: mint more than maximum"
    assets: uint256 = erc4626._preview_mint(shares)
    self._checkpoint(receiver)
    erc4626._deposit(msg.sender, receiver, assets, shares)
    self._check_min_supply()
    return assets


@external
@nonreentrant
def withdraw(assets: uint256, receiver: address, owner: address) -> uint256:
    """
    @notice Unstake `assets` LP to `receiver`, burning `owner`'s gauge shares.
    @dev Checkpoints rewards before the balance change.
    @param assets Amount of LP tokens to withdraw.
    @param receiver Recipient of the LP tokens.
    @param owner Account whose gauge shares are burned (allowance applies if not caller).
    @return Gauge shares burned.
    """
    assert assets <= erc4626._max_withdraw(owner), "erc4626: withdraw more than maximum"
    shares: uint256 = erc4626._preview_withdraw(assets)
    self._checkpoint(owner)
    erc4626._withdraw(msg.sender, receiver, owner, assets, shares)
    self._check_min_supply()
    return shares


@external
@nonreentrant
def redeem(shares: uint256, receiver: address, owner: address) -> uint256:
    """
    @notice Burn `owner`'s `shares` gauge tokens, returning LP to `receiver`.
    @dev Checkpoints rewards before the balance change.
    @param shares Amount of gauge shares to burn.
    @param receiver Recipient of the LP tokens.
    @param owner Account whose gauge shares are burned (allowance applies if not caller).
    @return LP tokens returned.
    """
    assert shares <= erc4626._max_redeem(owner), "erc4626: redeem more than maximum"
    assets: uint256 = erc4626._preview_redeem(shares)
    self._checkpoint(owner)
    erc4626._withdraw(msg.sender, receiver, owner, assets, shares)
    self._check_min_supply()
    return assets


@external
@nonreentrant
def transfer(to: address, amount: uint256) -> bool:
    """
    @notice ERC20 transfer of gauge shares; checkpoints rewards for both parties.
    @param to Recipient of the gauge shares.
    @param amount Amount of gauge shares to transfer.
    @return True on success.
    """
    self._checkpoint(msg.sender)
    self._checkpoint(to)
    erc4626.erc20._transfer(msg.sender, to, amount)
    return True


@external
@nonreentrant
def transferFrom(owner: address, to: address, amount: uint256) -> bool:
    """
    @notice ERC20 transferFrom of gauge shares; checkpoints rewards for both parties.
    @param owner Account to move gauge shares from (allowance applies).
    @param to Recipient of the gauge shares.
    @param amount Amount of gauge shares to transfer.
    @return True on success.
    """
    self._checkpoint(owner)
    self._checkpoint(to)
    erc4626.erc20._spend_allowance(owner, msg.sender, amount)
    erc4626.erc20._transfer(owner, to, amount)
    return True
