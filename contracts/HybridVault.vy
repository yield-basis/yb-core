# @version 0.4.3
# pragma nonreentrancy on
"""
@title HybridVaultFactory
@notice Factory for vaults which keep both YB vaults and scrvUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
"""
from ethereum.ercs import IERC20


struct Market:
    asset_token: IERC20
    cryptopool: CurveCryptoPool
    amm: AMM
    lt: LT
    price_oracle: address
    virtual_pool: address
    staker: IERC4626

struct OraclizedValue:
    p_o: uint256
    value: uint256

struct LiquidityValues:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256


interface IERC4626:
    def balanceOf(user: address) -> uint256: view
    def transfer(receiver: address, amount: uint256) -> bool: nonpayable
    def transferFrom(owner: address, receiver: address, amount: uint256) -> bool: nonpayable
    def previewRedeem(shares: uint256) -> uint256: view
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable
    def redeem(shares: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def claim(reward: IERC20, user: address) -> uint256: nonpayable
    def preview_claim(reward: IERC20, user: address) -> uint256: view

interface CurveCryptoPool:
    def price_scale() -> uint256: view

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256) -> uint256: nonpayable
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def withdraw(shares: uint256, min_assets: uint256, receiver: address) -> uint256: nonpayable
    def agg() -> PriceOracle: view
    def balanceOf(user: address) -> uint256: view
    def approve(_for: address, amount: uint256) -> bool: nonpayable
    def totalSupply() -> uint256: view
    def liquidity() -> LiquidityValues: view
    def stablecoin_allocation() -> uint256: view
    def stablecoin_allocated() -> uint256: view
    def allocate_stablecoins(limit: uint256): nonpayable

interface AMM:
    def get_debt() -> uint256: view
    def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> OraclizedValue: view
    def value_oracle() -> OraclizedValue: view

interface GaugeController:
    def is_killed(gauge: address) -> bool: view

interface Factory:
    def admin() -> address: view
    def markets(idx: uint256) -> Market: view

interface VaultFactory:
    def stablecoin_fraction() -> uint256: view
    def pool_limits(pool_id: uint256) -> uint256: view
    def lt_allocate_stablecoins(lt: LT, limit: uint256): nonpayable


MAX_VAULTS: public(constant(uint256)) = 16
FACTORY: public(immutable(Factory))
CRVUSD: public(immutable(IERC20))
CRVUSD_VAULT: public(immutable(IERC4626))
owner: public(address)
vault_factory: public(VaultFactory)
used_vaults: public(DynArray[uint256, MAX_VAULTS])

pool_approved: HashMap[uint256, bool]
token_in_use: HashMap[address, bool]
stablecoin_allocation: public(uint256)


@deploy
def __init__(factory: Factory, crvusd: IERC20, crvusd_vault: IERC4626):
    """
    @notice Initialize the HybridVault implementation contract
    @dev Sets owner to 0x01 to prevent initialization of the factory itself
    @param factory The YB factory contract address
    @param crvusd The crvUSD token address
    @param crvusd_vault The scrvUSD vault address
    """
    self.owner = 0x0000000000000000000000000000000000000001  # To prevent initializing the factory itself
    FACTORY = factory
    CRVUSD = crvusd
    CRVUSD_VAULT = crvusd_vault


@external
def initialize(user: address) -> bool:
    """
    @notice Initialize a cloned vault instance for a user
    @dev Can only be called once; sets the vault owner and approves crvUSD spending
    @param user The address that will own this vault
    @return True if initialization succeeded
    """
    assert self.owner == empty(address), "Already initialized"
    self.owner = user
    self.vault_factory = VaultFactory(msg.sender)
    extcall CRVUSD.approve(CRVUSD_VAULT.address, max_value(uint256))
    return True


@internal
@view
def _crvusd_available() -> uint256:
    return staticcall CRVUSD_VAULT.previewRedeem(staticcall CRVUSD_VAULT.balanceOf(self))


@internal
@view
def _downscale(amount: uint256) -> uint256:
    return amount * (staticcall self.vault_factory.stablecoin_fraction()) // 10**18


@internal
@view
def _required_crvusd() -> uint256:
    total_crvusd: uint256 = 0
    for pool_id: uint256 in self.used_vaults:
        pool: Market = staticcall FACTORY.markets(pool_id)
        lt_shares: uint256 = staticcall pool.lt.balanceOf(self) + staticcall pool.staker.previewRedeem(staticcall pool.staker.balanceOf(self))
        lt_total: uint256 = staticcall pool.lt.totalSupply()
        liquidity: LiquidityValues = staticcall pool.lt.liquidity()
        crvusd_amount: uint256 = (staticcall pool.amm.value_oracle()).value
        crvusd_amount = crvusd_amount * (liquidity.total - convert(max(liquidity.admin, 0), uint256)) // liquidity.total * lt_shares // lt_total
        total_crvusd += crvusd_amount
    return total_crvusd


@internal
@view
def _required_crvusd_for(lt: LT, amm: AMM, assets: uint256, debt: uint256) -> (uint256, uint256):
    # Only works when lt_supply > 0
    # Also probably make ceil div?
    lt_shares: uint256 = staticcall lt.preview_deposit(assets, debt, False)
    lt_supply: uint256 = staticcall lt.totalSupply()
    value_in_amm: uint256 = (staticcall amm.value_oracle()).value
    return value_in_amm, value_in_amm * lt_shares // lt_supply


@external
@view
def required_crvusd() -> uint256:
    """
    @notice Calculate total crvUSD required to back all vault positions
    @return The downscaled amount of crvUSD required
    """
    return self._downscale(self._required_crvusd())


@external
@view
def withdrawable_crvusd_for(pool_id: uint256, shares: uint256, is_staked: bool) -> uint256:
    """
    @notice Calculate crvUSD that can be freed up by withdrawing shares from a vault
    @param pool_id The market pool identifier
    @param shares Amount of shares to withdraw
    @param is_staked Whether the shares are staked to earn YB
    @return The amount of crvUSD that becomes withdrawable after burning these shares
    """
    market: Market = staticcall FACTORY.markets(pool_id)
    lt_shares: uint256 = shares
    if is_staked:
        lt_shares = staticcall market.staker.previewRedeem(shares)
    released_value: uint256 = (staticcall market.amm.value_oracle()).value
    required_value: uint256 = self._required_crvusd()
    required_value = self._downscale(required_value - min(required_value, released_value))
    crvusd_available: uint256 = self._crvusd_available()
    return max(crvusd_available, required_value) - required_value


@external
@view
def raw_required_crvusd_for(pool_id: uint256, assets: uint256, debt: uint256) -> uint256:
    """
    @notice Calculate crvUSD required for a potential deposit
    @param pool_id The market pool identifier
    @param assets Amount of collateral assets to deposit
    @param debt Amount of debt to take on
    @return The downscaled crvUSD amount required for this deposit
    """
    market: Market = staticcall FACTORY.markets(pool_id)
    return self._downscale(self._required_crvusd_for(market.lt, market.amm, assets, debt)[1])


@external
@view
def crvusd_for_deposit(pool_id: uint256, assets: uint256, debt: uint256) -> uint256:
    """
    @notice Calculate additional crvUSD needed from user for a deposit
    @param pool_id The market pool identifier
    @param assets Amount of collateral assets to deposit
    @param debt Amount of debt to take on
    @return The additional crvUSD the user must provide (0 if sufficient balance)
    """
    market: Market = staticcall FACTORY.markets(pool_id)
    available: uint256 = self._crvusd_available()
    required: uint256 = self._downscale(self._required_crvusd() + self._required_crvusd_for(market.lt, market.amm, assets, debt)[1])
    return required - min(required, available)


@internal
def _add_to_used(pool_id: uint256):
    used_vaults: DynArray[uint256, MAX_VAULTS] = self.used_vaults
    if pool_id not in used_vaults:
        used_vaults.append(pool_id)
        self.used_vaults = used_vaults


@internal
def _remove_from_used(pool_id: uint256):
    used_vaults: DynArray[uint256, MAX_VAULTS] = self.used_vaults
    new_used_vaults: DynArray[uint256, MAX_VAULTS] = empty(DynArray[uint256, MAX_VAULTS])
    for p: uint256 in used_vaults:
        if p != pool_id:
            new_used_vaults.append(p)
    self.used_vaults = new_used_vaults


@internal
def _allocate_stablecoins(lt: LT, limit: uint256):
    extcall self.vault_factory.lt_allocate_stablecoins(lt, limit)


@external
def deposit(pool_id: uint256, assets: uint256, debt: uint256, min_shares: uint256, stake: bool = False, deposit_stablecoins: bool = False) -> uint256:
    """
    @notice Deposit assets into a YB market through this vault
    @dev Approves tokens on first use; manages stablecoin allocation limits
    @param pool_id The market pool identifier
    @param assets Amount of assets to deposit
    @param debt Amount of debt to take on
    @param min_shares Minimum LT shares to receive (slippage protection)
    @param stake If True, automatically stake LT shares in the gauge
    @param deposit_stablecoins If True, pull additional crvUSD from sender if needed
    @return LT shares received (or staked shares if stake=True)
    """
    assert self.owner == msg.sender, "Access"  # XXX should we allow others to deposit for us? Seems safe?

    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address)
    if not self.pool_approved[pool_id]:
        assert extcall market.asset_token.approve(market.lt.address, max_value(uint256), default_return_value=True)
        extcall market.lt.approve(market.staker.address, max_value(uint256))
        self.pool_approved[pool_id] = True
        self.token_in_use[market.lt.address] = True
        self.token_in_use[market.staker.address] = True

    pool_value: uint256 = 0
    additional_crvusd: uint256 = 0
    pool_value, additional_crvusd = self._required_crvusd_for(market.lt, market.amm, assets, debt)
    crvusd_available: uint256 = self._crvusd_available()
    crvusd_required: uint256 = self._downscale(self._required_crvusd() + additional_crvusd)
    if crvusd_available < crvusd_required:
        if deposit_stablecoins:
            self._deposit_crvusd(crvusd_required - crvusd_available)
        else:
            raise "Not enough crvUSD"
    assert pool_value + additional_crvusd <= staticcall self.vault_factory.pool_limits(pool_id), "Beyond pool limit"

    # Temporarily make the cap bigger than necessary
    assert debt <= 11 * additional_crvusd // 10, "Debt made too high"
    previous_allocation: uint256 = staticcall market.lt.stablecoin_allocation()
    self._allocate_stablecoins(market.lt, max((pool_value + additional_crvusd) * 22 // 10, previous_allocation))

    if assets > 0:
        self._add_to_used(pool_id)

    assert extcall market.asset_token.transferFrom(msg.sender, self, assets, default_return_value=True)
    lt_shares: uint256 = extcall market.lt.deposit(assets, debt, min_shares)

    # Reduce cap to what it should be
    self._allocate_stablecoins(market.lt, previous_allocation + 2 * additional_crvusd)
    self.stablecoin_allocation += 2 * additional_crvusd

    if not stake:
        return lt_shares

    else:
        return extcall market.staker.deposit(lt_shares, self)


@external
def withdraw(pool_id: uint256, shares: uint256, min_assets: uint256, unstake: bool = False, receiver: address = msg.sender, withdraw_stablecoins: bool = False) -> uint256:
    """
    @notice Withdraw assets from a YB market
    @param pool_id The market pool identifier
    @param shares LT shares (or staked shares if unstake=True) to withdraw
    @param min_assets Minimum assets to receive (slippage protection)
    @param unstake If True, unstake from gauge before withdrawing
    @param receiver Address to receive the withdrawn assets
    @param withdraw_stablecoins If True, return excess crvUSD to sender
    @return Amount of assets withdrawn
    """
    assert self.owner == msg.sender, "Access"

    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address)

    required_before: uint256 = self._required_crvusd()

    lt_shares: uint256 = shares
    if unstake:
        lt_shares = extcall market.staker.redeem(shares, self, self)
    assets: uint256 = extcall market.lt.withdraw(lt_shares, min_assets, msg.sender)

    required_after: uint256 = self._required_crvusd()

    if required_before > required_after and withdraw_stablecoins:
        self._redeem_crvusd(required_before - required_after)

    previous_allocation: uint256 = staticcall market.lt.stablecoin_allocation()
    reduction: uint256 = min(2 * (required_before - required_after), self.stablecoin_allocation)
    self._allocate_stablecoins(market.lt, previous_allocation - reduction)
    self.stablecoin_allocation -= reduction

    return assets



@external
def stake(pool_id: uint256, pool_shares: uint256) -> uint256:
    """
    @notice Stake LT shares in the market's gauge
    @param pool_id The market pool identifier
    @param pool_shares Amount of LT shares to stake
    @return Amount of staked (gauge) shares received
    """
    assert self.owner == msg.sender, "Access"
    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address)
    return extcall market.staker.deposit(pool_shares, self)


@external
def unstake(pool_id: uint256, gauge_shares: uint256) -> uint256:
    """
    @notice Unstake shares from the market's gauge
    @param pool_id The market pool identifier
    @param gauge_shares Amount of staked shares to unstake
    @return Amount of LT shares received
    """
    assert self.owner == msg.sender, "Access"
    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address)
    return extcall market.staker.redeem(gauge_shares, self, self)


@external
@view
def preview_claim_reward(token: IERC20) -> uint256:
    """
    @notice Preview claimable rewards across all staked positions
    @param token The reward token to query
    @return Total claimable amount of the reward token
    """
    total: uint256 = 0
    for pool_id: uint256 in self.used_vaults:
        market: Market = staticcall FACTORY.markets(pool_id)
        total += staticcall market.staker.preview_claim(token, self)
    return total


@external
def claim_reward(token: IERC20) -> uint256:
    """
    @notice Claim rewards from all staked positions and send to owner
    @param token The reward token to claim
    @return Total amount claimed and transferred to owner
    """
    total: uint256 = 0
    for pool_id: uint256 in self.used_vaults:
        market: Market = staticcall FACTORY.markets(pool_id)
        total += extcall market.staker.claim(token, self)
    if total > 0:
        assert extcall token.transfer(self.owner, total, default_return_value=True)
    return total


@internal
def _deposit_crvusd(assets: uint256) -> uint256:
    extcall CRVUSD.transferFrom(msg.sender, self, assets)
    return extcall CRVUSD_VAULT.deposit(assets, self)


@external
def deposit_crvusd(assets: uint256) -> uint256:
    """
    @notice Deposit crvUSD into scrvUSD vault to back positions
    @param assets Amount of crvUSD to deposit
    @return Amount of scrvUSD shares received
    """
    return self._deposit_crvusd(assets)


@internal
def _redeem_crvusd(shares: uint256) -> uint256:
    withdrawn: uint256 = extcall CRVUSD_VAULT.redeem(shares, msg.sender, self)
    assert self._crvusd_available() >= self._downscale(self._required_crvusd()), "Not enough crvUSD left"
    return withdrawn


@external
def redeem_crvusd(shares: uint256) -> uint256:
    """
    @notice Redeem scrvUSD shares for crvUSD (owner only)
    @dev Reverts if withdrawal would leave insufficient backing
    @param shares Amount of scrvUSD shares to redeem
    @return Amount of crvUSD withdrawn
    """
    assert self.owner == msg.sender, "Access"
    return self._redeem_crvusd(shares)


@external
def deposit_scrvusd(shares: uint256):
    """
    @notice Deposit scrvUSD shares directly into the vault
    @param shares Amount of scrvUSD shares to transfer in
    """
    extcall CRVUSD_VAULT.transferFrom(msg.sender, self, shares)


@external
def withdraw_scrvusd(shares: uint256):
    """
    @notice Withdraw scrvUSD shares from the vault (owner only)
    @dev Reverts if withdrawal would leave insufficient backing
    @param shares Amount of scrvUSD shares to withdraw
    """
    assert self.owner == msg.sender, "Access"
    extcall CRVUSD_VAULT.transfer(msg.sender, shares)
    assert self._crvusd_available() >= self._downscale(self._required_crvusd()), "Not enough crvUSD left"


@external
def recover_tokens(token: IERC20):
    """
    @notice Recover accidentally sent tokens (owner only)
    @dev Cannot recover LT or staker tokens that are actively in use
    @param token The token to recover
    """
    assert self.owner == msg.sender, "Access"
    assert not self.token_in_use[token.address], "Token not allowed"
    assert extcall token.transfer(msg.sender, staticcall token.balanceOf(self), default_return_value=True)
