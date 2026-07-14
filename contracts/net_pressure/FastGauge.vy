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


# The staked LP may itself be a 2-coin Curve stableswap pool (self-LP), which enables the
# add_liquidity/remove_liquidity zap helpers below. Only 2-coin pools are supported.
POOL_N_COINS: constant(uint256) = 2


# Views + the single-coin exit share the same selector across pool generations.
interface StableswapPool:
    def coins(i: uint256) -> address: view
    def remove_liquidity_one_coin(amount: uint256, i: int128, min_received: uint256, receiver: address) -> uint256: nonpayable

# Newer (Stableswap-NG) pools take/return DynArray amounts.
interface StableswapPoolDyn:
    def add_liquidity(amounts: DynArray[uint256, POOL_N_COINS], min_mint_amount: uint256, receiver: address) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: DynArray[uint256, POOL_N_COINS], receiver: address) -> DynArray[uint256, POOL_N_COINS]: nonpayable

# Older pools take/return a fixed uint256[2].
interface StableswapPoolStatic:
    def add_liquidity(amounts: uint256[POOL_N_COINS], min_mint_amount: uint256, receiver: address) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: uint256[POOL_N_COINS], receiver: address) -> uint256[POOL_N_COINS]: nonpayable


initializes: erc4626


exports: (
    erc4626.erc20.totalSupply,
    erc4626.erc20.balanceOf,
    erc4626.erc20.approve,
    erc4626.erc20.allowance,
    erc4626.decimals,
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

event SetEmaTime:
    ema_time: uint256

event Claim:
    user: indexed(address)
    amount: uint256

event AddLiquidity:
    provider: indexed(address)
    receiver: indexed(address)
    amounts: DynArray[uint256, POOL_N_COINS]
    lp: uint256

event RemoveLiquidity:
    owner: indexed(address)
    receiver: indexed(address)
    shares: uint256
    amounts: DynArray[uint256, POOL_N_COINS]

event RemoveLiquidityOne:
    owner: indexed(address)
    receiver: indexed(address)
    shares: uint256
    coin: int128
    dy: uint256


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

# Zap wiring: set iff LP_TOKEN is itself a 2-coin Curve pool (self-LP), auto-detected in
# __init__. When enabled, add_liquidity/remove_liquidity/remove_liquidity_one_coin deposit
# into / withdraw from the pool and (un)stake in one call. POOL_IS_DYNARRAY selects the
# pool's add/remove ABI (newer NG DynArray vs older fixed uint256[2]).
ZAP_ENABLED: public(immutable(bool))
POOL_IS_DYNARRAY: public(immutable(bool))
COIN0: public(immutable(IERC20))
COIN1: public(immutable(IERC20))

# ERC20 metadata, exposed directly (not via the erc4626 module, whose String[25]/[5]
# bounds are too tight for a per-market name). Sizes follow the common ERC20 convention
# (name String[64], symbol String[32]); each gauge gets a distinct, market-specific name.
name: public(immutable(String[64]))
symbol: public(immutable(String[32]))

# The PID controller: the only address allowed to set the rate and the source the
# gauge pulls reward tokens from. Set by the DAO (owner) after deployment.
pid: public(address)

reward_rate: public(uint256)             # crvUSD per second, set by the PID
reward_integral: public(uint256)         # accumulated reward per share, 1e18
last_update: public(uint256)
reward_integral_for: public(HashMap[address, uint256])
claimable: public(HashMap[address, uint256])  # settled, unclaimed crvUSD per user

# Manipulation-resistant EMA of the staked LP (== totalSupply, shares are 1:1 with the
# LP). The controller reads tvl_ema() as the "sink" it has attracted, so the raw stake -
# which a flash deposit could inflate for a single block - must not be readable directly.
# Curve-cryptopool structure: each checkpoint folds the PREVIOUSLY recorded supply into
# the average and records the current one for next time, so a value only counts once it
# has survived into a later block. A flash deposit -> read -> withdraw therefore reads a
# pre-manipulation value (see _checkpoint_tvl / tvl_ema).
staked_ema: public(uint256)              # smoothed staked LP
staked_ema_ts: public(uint256)           # last EMA checkpoint timestamp
staked_last: public(uint256)             # supply recorded last checkpoint (fed into next)
ema_time: public(uint256)                # EMA smoothing time constant (s), DAO-settable


@deploy
def __init__(_name: String[50], _symbol: String[29], lp_token: IERC20, reward_token: IERC20,
             owner: address):
    """
    @notice Deploy a gauge staking `lp_token` and streaming `reward_token`.
    @param _name Sink-pool pair suffix; the token name is "YB FastGauge: {_name}"
           (e.g. _name="crvUSD/pyUSD" -> "YB FastGauge: crvUSD/pyUSD").
    @param _symbol Sink-pool suffix; the token symbol is "fg-{_symbol}".
    @param lp_token The Curve stableswap LP token staked here (the vault asset).
    @param reward_token The streamed reward token (crvUSD).
    @param owner DAO address that can set the PID.
    """
    # The erc4626 module still needs a name/symbol, but we expose our own (above) and do
    # not export the module's - so pass empty strings here. Pass 0 so the module's own
    # MIN_SHARES check is a no-op (MIN_SHARES = 1); we enforce MIN_TOTAL_SUPPLY below.
    erc4626.__init__("", "", lp_token, 0, "Just say no", "to EIP712")
    erc4626.ownable.owner = owner
    name = concat("YB FastGauge: ", _name)
    symbol = concat("fg-", _symbol)
    LP_TOKEN = lp_token
    REWARD_TOKEN = reward_token
    self.last_update = block.timestamp
    self.staked_ema_ts = block.timestamp
    # ~10 min half-life (866 == 600/ln2 for the 1/e constant), matching YBLendingOracleLL's
    # EMA_TIME. Only sets the multi-block manipulation cost / responsiveness - the flash
    # (single-block) resistance is structural. DAO-tunable via set_ema_time.
    self.ema_time = 866

    # --- zap detection: is the staked LP itself a 2-coin Curve pool? ---------
    # Probe coins(0); a plain LP token (or mock) has no such selector, so the zap helpers
    # stay disabled and the gauge is a pure LP-staking gauge.
    ok: bool = False
    ret: Bytes[32] = b""
    ok, ret = raw_call(lp_token.address,
                       abi_encode(empty(uint256), method_id=method_id("coins(uint256)")),
                       max_outsize=32, is_static_call=True, revert_on_failure=False)
    zap_enabled: bool = ok and len(ret) == 32
    coin0: IERC20 = empty(IERC20)
    coin1: IERC20 = empty(IERC20)
    is_dynarray: bool = False
    if zap_enabled:
        coin0 = IERC20(abi_decode(ret, address))
        coin1 = IERC20(staticcall StableswapPool(lp_token.address).coins(1))
        # One-time infinite approvals so add_liquidity can pull the coins into the pool.
        assert extcall coin0.approve(lp_token.address, max_value(uint256), default_return_value=True)
        assert extcall coin1.approve(lp_token.address, max_value(uint256), default_return_value=True)
        # Detect the pool's amounts ABI: only NG pools expose calc_token_amount(uint256[],bool).
        dyn_ok: bool = False
        dyn_ret: Bytes[32] = b""
        probe: DynArray[uint256, POOL_N_COINS] = [0, 0]
        dyn_ok, dyn_ret = raw_call(lp_token.address,
                                   abi_encode(probe, True,
                                              method_id=method_id("calc_token_amount(uint256[],bool)")),
                                   max_outsize=32, is_static_call=True, revert_on_failure=False)
        is_dynarray = dyn_ok
    ZAP_ENABLED = zap_enabled
    COIN0 = coin0
    COIN1 = coin1
    POOL_IS_DYNARRAY = is_dynarray


@internal
@view
def _check_min_supply():
    """
    @notice Enforce the seed-the-market floor: total supply is 0 or >= MIN_TOTAL_SUPPLY.
    """
    supply: uint256 = erc4626.erc20.totalSupply
    assert supply == 0 or supply >= MIN_TOTAL_SUPPLY, "Below min supply"


# --- staked-LP EMA (manipulation-resistant sink measure) ---------------------

@internal
@view
def _staked_ema() -> (uint256, bool):
    """
    @notice The staked-LP EMA projected to now.
    @dev Blends the previously recorded supply (self.staked_last) over the elapsed time
         into the stored average. Since staked_last is only ever the supply that stood at
         a PAST checkpoint, a stake deposited and withdrawn within one block never enters
         this value: within the same block dt == 0 (alpha == 1) so the stored average is
         returned unchanged. Returns (ema, advanced) where advanced is True iff time has
         passed since the last checkpoint (so the caller knows to persist it).
    @return (staked-LP EMA, advanced)
    """
    ts_last: uint256 = self.staked_ema_ts
    if block.timestamp <= ts_last:
        return (self.staked_ema, False)
    dt: uint256 = block.timestamp - ts_last
    alpha: uint256 = convert(math._wad_exp(-convert(dt * PRECISION // self.ema_time, int256)), uint256)
    return ((self.staked_last * (PRECISION - alpha) + self.staked_ema * alpha) // PRECISION, True)


@internal
def _checkpoint_tvl():
    """
    @notice Advance the staked-LP EMA, then record the current supply for the next update.
    @dev Must run AFTER a supply change so staked_last captures the post-change supply.
         The EMA is advanced using the OLD staked_last (Curve-cryptopool style), so the
         just-changed supply only starts counting from the next checkpoint onward.
    """
    ema: uint256 = 0
    advanced: bool = False
    ema, advanced = self._staked_ema()
    if advanced:
        self.staked_ema = ema
        self.staked_ema_ts = block.timestamp
    self.staked_last = erc4626.erc20.totalSupply


@external
@view
def tvl_ema() -> uint256:
    """
    @notice Manipulation-resistant EMA of the LP staked here (LP-token units).
    @dev The controller's "sink" measure. Flash-proof: a stake inflated and removed within
         a single block does not move this value (see _staked_ema).
    @return Smoothed staked LP amount.
    """
    return self._staked_ema()[0]


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


@external
def set_ema_time(ema_time: uint256):
    """
    @notice Set the staked-LP EMA smoothing time constant (seconds). DAO only.
    @dev Settles the EMA at the old constant first. Larger == smoother / harder to move
         across blocks but slower to track genuine sink changes.
    @param ema_time New smoothing time constant in seconds (> 0).
    """
    erc4626.ownable._check_owner()
    assert ema_time > 0, "ema_time"
    self._checkpoint_tvl()
    self.ema_time = ema_time
    log SetEmaTime(ema_time=ema_time)


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
    self._checkpoint_tvl()
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
    self._checkpoint_tvl()
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
    self._checkpoint_tvl()
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
    self._checkpoint_tvl()
    return assets


# --- zap: deposit into / withdraw from the pool and (un)stake in one call ----

@external
@nonreentrant
def add_liquidity(amounts: DynArray[uint256, POOL_N_COINS], min_mint_amount: uint256,
                  receiver: address) -> uint256:
    """
    @notice Deposit `amounts` of the pool's two coins into the pool and stake the minted LP
            here, minting gauge shares to `receiver` - all in one transaction.
    @dev Pulls the coins from the caller (approve this gauge first). Slippage-bounded by
         min_mint_amount. Shares are 1:1 with the staked LP.
    @param amounts Coin amounts to deposit ([coin0, coin1]); either may be 0.
    @param min_mint_amount Minimum LP (== gauge shares) to mint, else revert.
    @param receiver Recipient of the gauge shares.
    @return Gauge shares minted (== LP staked).
    """
    assert ZAP_ENABLED, "No zap: LP is not a pool"
    assert len(amounts) == POOL_N_COINS, "Bad amounts length"
    if amounts[0] > 0:
        assert extcall COIN0.transferFrom(msg.sender, self, amounts[0], default_return_value=True)
    if amounts[1] > 0:
        assert extcall COIN1.transferFrom(msg.sender, self, amounts[1], default_return_value=True)

    # Add liquidity with this gauge as the LP receiver (approvals set in __init__).
    lp: uint256 = 0
    if POOL_IS_DYNARRAY:
        lp = extcall StableswapPoolDyn(LP_TOKEN.address).add_liquidity(amounts, min_mint_amount, self)
    else:
        static_amounts: uint256[POOL_N_COINS] = [amounts[0], amounts[1]]
        lp = extcall StableswapPoolStatic(LP_TOKEN.address).add_liquidity(static_amounts, min_mint_amount, self)

    # Stake the freshly minted LP (already held here): mint 1:1 gauge shares to receiver.
    self._checkpoint(receiver)
    erc4626.erc20._mint(receiver, lp)
    self._check_min_supply()
    self._checkpoint_tvl()
    log AddLiquidity(provider=msg.sender, receiver=receiver, amounts=amounts, lp=lp)
    return lp


@external
@nonreentrant
def remove_liquidity(shares: uint256, min_amounts: DynArray[uint256, POOL_N_COINS],
                     receiver: address) -> DynArray[uint256, POOL_N_COINS]:
    """
    @notice Unstake `shares` gauge tokens and remove the underlying LP from the pool as both
            coins, sent to `receiver` - all in one transaction.
    @dev Burns the caller's gauge shares (1:1 with LP). Slippage-bounded by min_amounts.
    @param shares Gauge shares to burn (== LP to remove).
    @param min_amounts Minimum per-coin output ([coin0, coin1]), else revert.
    @param receiver Recipient of the withdrawn coins.
    @return Coin amounts sent to `receiver`.
    """
    assert ZAP_ENABLED, "No zap: LP is not a pool"
    assert len(min_amounts) == POOL_N_COINS, "Bad min_amounts length"
    self._checkpoint(msg.sender)
    erc4626.erc20._burn(msg.sender, shares)
    self._check_min_supply()

    out: DynArray[uint256, POOL_N_COINS] = []
    if POOL_IS_DYNARRAY:
        out = extcall StableswapPoolDyn(LP_TOKEN.address).remove_liquidity(shares, min_amounts, receiver)
    else:
        static_min: uint256[POOL_N_COINS] = [min_amounts[0], min_amounts[1]]
        got: uint256[POOL_N_COINS] = extcall StableswapPoolStatic(
            LP_TOKEN.address).remove_liquidity(shares, static_min, receiver)
        out = [got[0], got[1]]

    self._checkpoint_tvl()
    log RemoveLiquidity(owner=msg.sender, receiver=receiver, shares=shares, amounts=out)
    return out


@external
@nonreentrant
def remove_liquidity_one_coin(shares: uint256, i: int128, min_received: uint256,
                              receiver: address) -> uint256:
    """
    @notice Unstake `shares` gauge tokens and remove the underlying LP from the pool as a
            single coin `i`, sent to `receiver` - all in one transaction.
    @dev Burns the caller's gauge shares (1:1 with LP). Slippage-bounded by min_received.
    @param shares Gauge shares to burn (== LP to remove).
    @param i Coin index to receive (0 or 1).
    @param min_received Minimum coin output, else revert.
    @param receiver Recipient of the withdrawn coin.
    @return Coin amount sent to `receiver`.
    """
    assert ZAP_ENABLED, "No zap: LP is not a pool"
    self._checkpoint(msg.sender)
    erc4626.erc20._burn(msg.sender, shares)
    self._check_min_supply()

    # Same selector across pool generations (no amounts array).
    dy: uint256 = extcall StableswapPool(LP_TOKEN.address).remove_liquidity_one_coin(
        shares, i, min_received, receiver)
    self._checkpoint_tvl()
    log RemoveLiquidityOne(owner=msg.sender, receiver=receiver, shares=shares, coin=i, dy=dy)
    return dy


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
