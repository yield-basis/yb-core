# @version 0.4.3
# pragma nonreentrancy on
"""
@title HybridVault
@notice Vault that combines YB market positions with a crvUSD vault
@author Yield Basis
@license GNU Affero General Public License v3.0
"""
from ethereum.ercs import IERC20
from ethereum.ercs import IERC20Detailed


struct Market:
    asset_token: IERC20
    cryptopool: CurveCryptoPool
    amm: AMM
    lt: LT
    price_oracle: PriceOracle
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
    def withdraw(assets: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def redeem(shares: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def claim(reward: IERC20, user: address) -> uint256: nonpayable
    def preview_claim(reward: IERC20, user: address) -> uint256: view

interface CurveCryptoPool:
    def price_oracle() -> uint256: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256) -> uint256: nonpayable
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def withdraw(shares: uint256, min_assets: uint256, receiver: address) -> uint256: nonpayable
    def emergency_withdraw(shares: uint256, receiver: address, owner: address) -> (uint256, int256): nonpayable
    def balanceOf(user: address) -> uint256: view
    def approve(_for: address, amount: uint256) -> bool: nonpayable
    def totalSupply() -> uint256: view
    def liquidity() -> LiquidityValues: view
    def stablecoin_allocation() -> uint256: view
    def is_killed() -> bool: view

interface AMM:
    def value_oracle() -> OraclizedValue: view
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view

interface Factory:
    def markets(idx: uint256) -> Market: view

interface VaultFactory:
    def ADMIN() -> address: view
    def stablecoin_fraction() -> uint256: view
    def pool_limits(pool_id: uint256) -> uint256: view
    def allowed_crvusd_vaults(vault: address) -> bool: view
    def lt_allocate_stablecoins(lt: LT, limit: uint256): nonpayable
    def update_vault_required(crvusd_vault: address, new_required: uint256, check_limit: bool): nonpayable

interface PriceOracle:
    def price() -> uint256: view


event SetPersonalLimit:
    pool_id: uint256
    limit: uint256

event SetCrvusdVault:
    vault: IERC4626


AMM_MIN_SAFE_DEBT: public(immutable(uint256))
AMM_MAX_SAFE_DEBT: public(immutable(uint256))

MAX_VAULTS: public(constant(uint256)) = 16
FACTORY: public(immutable(Factory))
CRVUSD: public(immutable(IERC20))
VAULT_FACTORY: public(immutable(VaultFactory))
owner: public(address)
crvusd_vault: public(IERC4626)
used_vaults: public(DynArray[uint256, MAX_VAULTS])

pool_approved: HashMap[uint256, bool]
token_in_use: HashMap[address, bool]
stablecoin_allocation: public(HashMap[uint256, uint256])
personal_limit: public(HashMap[uint256, uint256])


@deploy
def __init__(factory: Factory, crvusd: IERC20, vault_factory: VaultFactory):
    """
    @notice Initialize the HybridVault implementation contract
    @dev Sets owner to 0x01 to prevent initialization of the implementation itself
    @param factory The YB factory contract address
    @param crvusd The crvUSD token address
    @param vault_factory The HybridVaultFactory contract address
    """
    self.owner = 0x0000000000000000000000000000000000000001  # To prevent initializing the factory itself
    FACTORY = factory
    CRVUSD = crvusd
    VAULT_FACTORY = vault_factory

    leverage: uint256 = 2 * 10**18
    denominator: uint256 = 2 * leverage - 10**18
    # 1 / (4 * L**2)
    AMM_MIN_SAFE_DEBT = 10**54 // (4 * leverage**2)
    # (2 * L - 1)**2 / (4 * L**2) - 1 / (8 * L**2)
    AMM_MAX_SAFE_DEBT = denominator**2 * 10**18 // (4 * leverage**2) - 10**54 // (8 * leverage**2)


@external
def initialize(user: address, crvusd_vault: IERC4626) -> bool:
    """
    @notice Initialize a cloned vault instance for a user
    @dev Can only be called once; sets the vault owner and approves crvUSD spending
    @param user The address that will own this vault
    @param crvusd_vault The crvUSD vault (e.g., scrvUSD) to use for this vault
    @return True if initialization succeeded
    """
    assert self.owner == empty(address), "Already initialized"
    self.owner = user
    self.crvusd_vault = crvusd_vault
    extcall CRVUSD.approve(crvusd_vault.address, max_value(uint256))
    return True


@external
def set_personal_limit(pool_id: uint256, limit: uint256):
    """
    @notice Set a personal pool limit for this vault
    @param pool_id The market pool identifier
    @param limit The personal limit to set (added to the global pool limit)
    """
    assert msg.sender == staticcall VAULT_FACTORY.ADMIN(), "Only admin"
    self.personal_limit[pool_id] = limit
    log SetPersonalLimit(pool_id=pool_id, limit=limit)


@external
def set_crvusd_vault(new_vault: IERC4626, redeem: bool = True):
    """
    @notice Change the crvUSD vault used by this HybridVault
    @dev Only callable by owner when there are no active positions.
         Transfers old vault tokens back to the owner (redeemed as crvUSD by default).
    @param new_vault The new crvUSD vault to use (must be in allowed list)
    @param redeem If True, redeem old vault shares for crvUSD; if False, transfer vault shares directly
    """
    assert msg.sender == self.owner, "Only owner"
    assert staticcall VAULT_FACTORY.allowed_crvusd_vaults(new_vault.address), "Vault not allowed"
    assert len(self.used_vaults) == 0, "Has active positions"
    old_vault: IERC4626 = self.crvusd_vault
    if old_vault != empty(IERC4626):
        extcall VAULT_FACTORY.update_vault_required(old_vault.address, 0, False)
        shares: uint256 = staticcall old_vault.balanceOf(self)
        if shares > 0:
            if redeem:
                extcall old_vault.redeem(shares, msg.sender, self)
            else:
                extcall old_vault.transfer(msg.sender, shares)
        extcall CRVUSD.approve(old_vault.address, 0)
    self.crvusd_vault = new_vault
    extcall CRVUSD.approve(new_vault.address, max_value(uint256))
    log SetCrvusdVault(vault=new_vault)


@internal
@view
def _crvusd_available() -> uint256:
    if self.crvusd_vault != empty(IERC4626):
        shares: uint256 = staticcall self.crvusd_vault.balanceOf(self)
        if shares == 0:
            return 0
        return staticcall self.crvusd_vault.previewRedeem(shares)
    else:
        return 0


@internal
@view
def _downscale(amount: uint256) -> uint256:
    return amount * (staticcall VAULT_FACTORY.stablecoin_fraction()) // 10**18


@internal
@view
def _pool_limits(pool_id: uint256) -> uint256:
    return self.personal_limit[pool_id] + staticcall VAULT_FACTORY.pool_limits(pool_id)


@internal
@view
def _check_safe_limits(market: Market, assets: uint256, debt: uint256) -> bool:
    """
    @notice Check whether the AMM state would be within safe limits after a deposit
    @param market The market containing the AMM to check
    @param assets Amount of assets to deposit (0 to check current state)
    @param debt Amount of debt to take on (0 to check current state)
    @return True if within safe limits, False otherwise
    """
    # Calculate LP tokens from assets and debt
    lp_tokens: uint256 = 0
    if assets > 0 or debt > 0:
        lp_tokens = staticcall market.cryptopool.calc_token_amount([debt, assets], True)

    # Get current state and add new values
    collateral: uint256 = staticcall market.amm.collateral_amount() + lp_tokens
    total_debt: uint256 = staticcall market.amm.get_debt() + debt
    p_o: uint256 = staticcall market.price_oracle.price()

    # Collateral value in stablecoin terms (collateral precision is 10**18 for LT tokens)
    coll_value: uint256 = p_o * collateral // 10**18

    # Check safe limits
    if total_debt < coll_value * AMM_MIN_SAFE_DEBT // 10**18:
        return False
    if total_debt > coll_value * AMM_MAX_SAFE_DEBT // 10**18:
        return False
    return True


@external
@view
def safe_to_deposit(pool_id: uint256, assets: uint256, debt: uint256) -> bool:
    """
    @notice Check whether it is safe to deposit into a given market
    @dev Returns False if the AMM's debt/collateral ratio would be outside safe bounds after the deposit
    @param pool_id The market pool identifier
    @param assets Amount of assets to deposit (0 to check current state)
    @param debt Amount of debt to take on (0 to check current state)
    @return True if the AMM would be within safe limits, False otherwise
    """
    return self._check_safe_limits(staticcall FACTORY.markets(pool_id), assets, debt)


@external
@view
def pool_limits(pool_id: uint256) -> uint256:
    """
    @notice Get the effective pool limit for a specific market
    @dev Returns the sum of personal limit and global factory limit
    @param pool_id The market pool identifier
    @return The effective pool limit in crvUSD value
    """
    return self._pool_limits(pool_id)


@internal
@view
def _pool_crvusd(pool: Market) -> uint256:
    staker_balance: uint256 = staticcall pool.staker.balanceOf(self)
    lt_shares: uint256 = staticcall pool.lt.balanceOf(self)
    if staker_balance > 0:
        lt_shares += staticcall pool.staker.previewRedeem(staker_balance)
    if lt_shares == 0:
        return 0
    lt_total: uint256 = staticcall pool.lt.totalSupply()
    if lt_total == 0:
        return 0
    liquidity: LiquidityValues = staticcall pool.lt.liquidity()
    if liquidity.total == 0:
        return 0
    success: bool = False
    res: Bytes[64] = empty(Bytes[64])
    success, res = raw_call(
        pool.amm.address,
        method_id("value_oracle()"),
        max_outsize=64,
        is_static_call=True,
        revert_on_failure=False)
    if not success:
        return max_value(uint256)
    crvusd_amount: uint256 = abi_decode(res, (uint256, uint256))[1]
    crvusd_amount = crvusd_amount * (liquidity.total - convert(max(liquidity.admin, 0), uint256)) // liquidity.total * lt_shares // lt_total
    return crvusd_amount


@internal
@view
def _required_crvusd() -> uint256:
    total_crvusd: uint256 = 0
    for pool_id: uint256 in self.used_vaults:
        pool: Market = staticcall FACTORY.markets(pool_id)
        crvusd_amount: uint256 = self._pool_crvusd(pool)
        if crvusd_amount == max_value(uint256):
            return max_value(uint256)
        total_crvusd += crvusd_amount
    return total_crvusd


@internal
@view
def _required_crvusd_for(market: Market, assets: uint256, debt: uint256) -> (uint256, uint256):
    lt_shares: uint256 = staticcall market.lt.preview_deposit(assets, debt, False)
    lt_supply: uint256 = staticcall market.lt.totalSupply()
    liquidity: LiquidityValues = staticcall market.lt.liquidity()
    value_in_amm: uint256 = (staticcall market.amm.value_oracle()).value
    return value_in_amm, value_in_amm * (liquidity.total - convert(max(liquidity.admin, 0), uint256)) // liquidity.total * lt_shares // lt_supply


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
    lt_supply: uint256 = staticcall market.lt.totalSupply()
    released_value: uint256 = (staticcall market.amm.value_oracle()).value * lt_shares // lt_supply
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
    return self._downscale(self._required_crvusd_for(market, assets, debt)[1])


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
    required: uint256 = self._downscale(self._required_crvusd() + self._required_crvusd_for(market, assets, debt)[1])
    return required - min(required, available)


@internal
def _add_to_used(pool_id: uint256):
    used_vaults: DynArray[uint256, MAX_VAULTS] = self.used_vaults
    if pool_id not in used_vaults:
        used_vaults.append(pool_id)
        self.used_vaults = used_vaults


@internal
def _remove_from_used(pool_id: uint256):
    remaining_allocation: uint256 = self.stablecoin_allocation[pool_id]
    if remaining_allocation > 0:
        market: Market = staticcall FACTORY.markets(pool_id)
        previous_allocation: uint256 = staticcall market.lt.stablecoin_allocation()
        self._allocate_stablecoins(market.lt, previous_allocation - min(remaining_allocation, previous_allocation))
        self.stablecoin_allocation[pool_id] = 0

    used_vaults: DynArray[uint256, MAX_VAULTS] = self.used_vaults
    new_used_vaults: DynArray[uint256, MAX_VAULTS] = empty(DynArray[uint256, MAX_VAULTS])
    for p: uint256 in used_vaults:
        if p != pool_id:
            new_used_vaults.append(p)
    self.used_vaults = new_used_vaults


@internal
def _allocate_stablecoins(lt: LT, limit: uint256):
    extcall VAULT_FACTORY.lt_allocate_stablecoins(lt, limit)


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
    assert self.owner == msg.sender, "Access"

    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address), "Bad pool_id"
    if not self.pool_approved[pool_id]:
        assert extcall market.asset_token.approve(market.lt.address, max_value(uint256), default_return_value=True)
        extcall market.lt.approve(market.staker.address, max_value(uint256))
        self.pool_approved[pool_id] = True
        self.token_in_use[market.lt.address] = True
        self.token_in_use[market.staker.address] = True

    # Trigger checkpoint_staker_rebase() by staking 0 tokens
    extcall market.staker.deposit(0, self)

    pool_value: uint256 = 0
    additional_crvusd: uint256 = 0
    pool_value, additional_crvusd = self._required_crvusd_for(market, assets, debt)
    crvusd_available: uint256 = self._crvusd_available()
    # next line will revert if max_value(uint256)
    crvusd_required: uint256 = self._downscale(self._required_crvusd() + additional_crvusd)
    if crvusd_available < crvusd_required:
        if deposit_stablecoins:
            self._deposit_crvusd(crvusd_required - crvusd_available)
        else:
            raise "Not enough crvUSD"
    assert pool_value + additional_crvusd <= self._pool_limits(pool_id), "Beyond pool limit"

    # Temporarily make the cap bigger than necessary
    assert debt <= 11 * additional_crvusd // 10, "Debt made too high"
    previous_allocation: uint256 = staticcall market.lt.stablecoin_allocation()
    self._allocate_stablecoins(market.lt, max((pool_value + additional_crvusd) * 22 // 10, previous_allocation))

    if assets > 0:
        self._add_to_used(pool_id)

    assert extcall market.asset_token.transferFrom(msg.sender, self, assets, default_return_value=True)
    lt_shares: uint256 = extcall market.lt.deposit(assets, debt, min_shares)

    assert lt_shares > 0, "No liquidity given"

    # Reduce cap to what it should be
    self._allocate_stablecoins(market.lt, previous_allocation + 2 * additional_crvusd)
    self.stablecoin_allocation[pool_id] += 2 * additional_crvusd

    extcall VAULT_FACTORY.update_vault_required(self.crvusd_vault.address, crvusd_required, True)

    if not stake:
        return lt_shares

    else:
        return extcall market.staker.deposit(lt_shares, self)


@external
def withdraw(pool_id: uint256, shares: uint256, min_assets: uint256, unstake: bool = False, receiver: address = msg.sender, withdraw_stablecoins: bool = False) -> uint256:
    """
    @notice Withdraw assets from a YB market
    @param pool_id The market pool identifier
    @param shares LT shares (or staked shares if unstake=True) to withdraw; max_value(uint256) to withdraw all
    @param min_assets Minimum assets to receive (slippage protection)
    @param unstake If True, unstake from gauge before withdrawing
    @param receiver Address to receive the withdrawn assets
    @param withdraw_stablecoins If True, return excess crvUSD to sender
    @return Amount of assets withdrawn
    """
    assert self.owner == msg.sender, "Access"

    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address), "Bad pool_id"

    required_before: uint256 = self._required_crvusd()
    pool_crvusd_before: uint256 = self._pool_crvusd(market)

    lt_shares: uint256 = shares
    if unstake:
        staker_shares: uint256 = shares
        if staker_shares == max_value(uint256):
            staker_shares = staticcall market.staker.balanceOf(self)
        lt_shares = extcall market.staker.redeem(staker_shares, self, self)
    elif lt_shares == max_value(uint256):
        lt_shares = staticcall market.lt.balanceOf(self)
    lt_supply: uint256 = staticcall market.lt.totalSupply()

    assets: uint256 = extcall market.lt.withdraw(lt_shares, min_assets, receiver)

    removed: bool = False
    if staticcall market.lt.balanceOf(self) == 0 and staticcall market.staker.balanceOf(self) == 0:
        self._remove_from_used(pool_id)
        removed = True

    required_after: uint256 = self._required_crvusd()

    if not removed:
        previous_allocation: uint256 = staticcall market.lt.stablecoin_allocation()
        reduction: uint256 = 0

        if required_before == max_value(uint256) or required_after == max_value(uint256):
            pool_crvusd_after: uint256 = self._pool_crvusd(market)

            assert pool_crvusd_before != max_value(uint256) and pool_crvusd_after != max_value(uint256), "Oracle is broken"
            assert not withdraw_stablecoins, "Cannot withdraw stables"

            if pool_crvusd_before > pool_crvusd_after:
                reduction = min(2 * (pool_crvusd_before - pool_crvusd_after), self.stablecoin_allocation[pool_id])

        else:
            if required_before > required_after:
                if withdraw_stablecoins:
                    if required_after > 0:
                        self._withdraw_crvusd(self._downscale(required_before - required_after), receiver, True)
                    else:
                        self._redeem_crvusd(staticcall self.crvusd_vault.balanceOf(self), receiver)
                reduction = min(2 * (required_before - required_after), self.stablecoin_allocation[pool_id])

        if reduction > 0:
            if reduction > previous_allocation:
                reduction = previous_allocation
            self._allocate_stablecoins(market.lt, previous_allocation - reduction)
            self.stablecoin_allocation[pool_id] -= reduction

    else:
        # Pool fully withdrawn and removed - allocation already cleared by _remove_from_used
        if withdraw_stablecoins:
            assert required_after != max_value(uint256), "Cannot withdraw stables"
            if required_after > 0:
                available: uint256 = self._crvusd_available()
                needed: uint256 = self._downscale(required_after)
                if available > needed:
                    self._withdraw_crvusd(available - needed, receiver, True)
            else:
                self._redeem_crvusd(staticcall self.crvusd_vault.balanceOf(self), receiver)

    if required_after != max_value(uint256):
        extcall VAULT_FACTORY.update_vault_required(self.crvusd_vault.address, self._downscale(required_after), False)

    return assets



@external
def emergency_withdraw(pool_id: uint256, shares: uint256, crvusd_from_wallet: bool = False):
    """
    @notice Emergency withdraw from a YB market
    @dev Handles negative stables_to_return by redeeming crvUSD from the backing vault.
         Unstake LT tokens before calling this function if needed.
    @param pool_id The market pool identifier
    @param shares Amount of LT shares to withdraw; max_value(uint256) to withdraw all
    @param crvusd_from_wallet If True, pull necessary crvUSD from the caller instead of the backing vault
    """
    assert self.owner == msg.sender, "Access"

    market: Market = staticcall FACTORY.markets(pool_id)
    assert market.lt.address != empty(address), "Bad pool_id"

    lt_shares: uint256 = shares
    if lt_shares == max_value(uint256):
        lt_shares = staticcall market.lt.balanceOf(self)
    assert lt_shares > 0, "Zero shares"
    lt_supply: uint256 = staticcall market.lt.totalSupply()

    required_before: uint256 = self._required_crvusd()
    pool_crvusd_before: uint256 = self._pool_crvusd(market)

    crvusd_vault: IERC4626 = self.crvusd_vault
    if crvusd_from_wallet:
        # Pull crvUSD from caller instead of redeeming from backing vault
        total_debt: uint256 = staticcall market.amm.get_debt()
        if total_debt > 0:
            max_needed: uint256 = total_debt * lt_shares // lt_supply + 1
            extcall CRVUSD.transferFrom(msg.sender, self, max_needed)
    else:
        # Redeem all crvUSD from backing vault to cover potential debt repayment
        crvusd_shares: uint256 = staticcall crvusd_vault.balanceOf(self)
        if crvusd_shares > 0:
            extcall crvusd_vault.redeem(crvusd_shares, self, self)

    # Approve LT to pull crvUSD (for negative stables_to_return)
    extcall CRVUSD.approve(market.lt.address, max_value(uint256))
    crvusd_before: uint256 = staticcall CRVUSD.balanceOf(self)

    assets: uint256 = (extcall market.lt.emergency_withdraw(lt_shares, self, self))[0]

    # Reset crvUSD approval
    extcall CRVUSD.approve(market.lt.address, 0)

    # Handle remaining crvUSD
    remaining_crvusd: uint256 = staticcall CRVUSD.balanceOf(self)
    if crvusd_from_wallet:
        # Refund excess crvUSD to caller
        if remaining_crvusd > 0:
            extcall CRVUSD.transfer(msg.sender, remaining_crvusd)
    else:
        # Re-deposit remaining crvUSD back to the backing vault
        if remaining_crvusd > 0 and crvusd_vault.address != empty(address):
            extcall crvusd_vault.deposit(remaining_crvusd, self)

    # Send assets to owner
    _owner: address = self.owner
    if assets > 0:
        assert extcall market.asset_token.transfer(_owner, assets, default_return_value=True)

    # Check if pool is fully withdrawn and remove early
    removed: bool = False
    if staticcall market.lt.balanceOf(self) == 0 and staticcall market.staker.balanceOf(self) == 0:
        self._remove_from_used(pool_id)
        removed = True

    required_after: uint256 = self._required_crvusd()

    if not removed:
        # Reduce stablecoin allocation
        previous_allocation: uint256 = staticcall market.lt.stablecoin_allocation()
        reduction: uint256 = 0

        if required_before == max_value(uint256) or required_after == max_value(uint256):
            pool_crvusd_after: uint256 = self._pool_crvusd(market)
            if pool_crvusd_before != max_value(uint256) and pool_crvusd_after != max_value(uint256):
                # Use per-pool crvusd change to calculate reduction
                if pool_crvusd_before > pool_crvusd_after:
                    reduction = min(2 * (pool_crvusd_before - pool_crvusd_after), self.stablecoin_allocation[pool_id])
            else:
                # value_oracle() reverted for this pool!
                # therefore we reduce allocation for this pool more than otherwise
                # but it's fair b/c oracle failure begs for shutting it down
                stablecoin_fraction: uint256 = staticcall VAULT_FACTORY.stablecoin_fraction()
                reduction = crvusd_before - min(remaining_crvusd, crvusd_before)
                if stablecoin_fraction > 0:
                    reduction = reduction * 10**18 // stablecoin_fraction
                reduction = min(reduction, self.stablecoin_allocation[pool_id])
        else:
            if required_before > required_after:
                reduction = min(2 * (required_before - required_after), self.stablecoin_allocation[pool_id])

        if reduction > 0:
            if reduction > previous_allocation:
                reduction = previous_allocation
            self._allocate_stablecoins(market.lt, previous_allocation - reduction)
            old_allocation: uint256 = self.stablecoin_allocation[pool_id]
            # in fact, we NEVER can undeflow in the subtraction
            # but why not do this way just in case: extra safety never hurts
            self.stablecoin_allocation[pool_id] = old_allocation - min(reduction, old_allocation)

    if required_after != max_value(uint256):
        extcall VAULT_FACTORY.update_vault_required(self.crvusd_vault.address, self._downscale(required_after), False)


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
    assert market.lt.address != empty(address), "Bad pool_id"
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
    assert market.lt.address != empty(address), "Bad pool_id"
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
        success: bool = False
        res: Bytes[32] = empty(Bytes[32])
        success, res = raw_call(
            market.staker.address,
            abi_encode(token.address, self, method_id=method_id("preview_claim(address,address)")),
            max_outsize=32,
            is_static_call=True,
            revert_on_failure=False)
        if success:
            total += convert(res, uint256)
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
        success: bool = False
        res: Bytes[32] = empty(Bytes[32])
        success, res = raw_call(
            market.staker.address,
            abi_encode(token.address, self, method_id=method_id("claim(address,address)")),
            max_outsize=32,
            revert_on_failure=False)
        if success:
            total += convert(res, uint256)
    if total > 0:
        assert extcall token.transfer(self.owner, total, default_return_value=True)
    return total


@internal
def _deposit_crvusd(assets: uint256) -> uint256:
    extcall CRVUSD.transferFrom(msg.sender, self, assets)
    return extcall self.crvusd_vault.deposit(assets, self)


@external
def deposit_crvusd(assets: uint256) -> uint256:
    """
    @notice Deposit crvUSD into the crvUSD vault
    @param assets Amount of crvUSD to deposit
    @return Amount of crvUSD vault shares received
    """
    return self._deposit_crvusd(assets)


@internal
def _withdraw_crvusd(assets: uint256, receiver: address, post_check_crvusd: bool) -> uint256:
    to_withdraw: uint256 = min(assets, self._crvusd_available())
    if to_withdraw == 0:
        return 0
    shares_burned: uint256 = extcall self.crvusd_vault.withdraw(to_withdraw, receiver, self)
    if post_check_crvusd:
        assert self._crvusd_available() >= self._downscale(self._required_crvusd()), "Not enough crvUSD left"
    return shares_burned


@internal
def _redeem_crvusd(shares: uint256, receiver: address) -> uint256:
    withdrawn: uint256 = extcall self.crvusd_vault.redeem(shares, receiver, self)
    assert self._crvusd_available() >= self._downscale(self._required_crvusd()), "Not enough crvUSD left"
    return withdrawn


@external
def redeem_crvusd(shares: uint256) -> uint256:
    """
    @notice Redeem crvUSD vault shares for crvUSD (owner only)
    @dev Reverts if withdrawal would leave insufficient backing
    @param shares Amount of crvUSD vault shares to redeem
    @return Amount of crvUSD withdrawn
    """
    assert self.owner == msg.sender, "Access"
    return self._redeem_crvusd(shares, msg.sender)


@external
def deposit_scrvusd(shares: uint256):
    """
    @notice Deposit crvUSD vault shares directly into the vault
    @param shares Amount of crvUSD vault shares to transfer in
    """
    extcall self.crvusd_vault.transferFrom(msg.sender, self, shares)


@external
def withdraw_scrvusd(shares: uint256):
    """
    @notice Withdraw crvUSD vault shares from the vault (owner only)
    @dev Reverts if withdrawal would leave insufficient backing
    @param shares Amount of crvUSD vault shares to withdraw
    """
    assert self.owner == msg.sender, "Access"
    extcall self.crvusd_vault.transfer(msg.sender, shares)
    assert self._crvusd_available() >= self._downscale(self._required_crvusd()), "Not enough crvUSD left"


@external
def recover_tokens(token: IERC20):
    """
    @notice Recover accidentally sent tokens (owner only)
    @dev Cannot recover LT or staker tokens that are actively in use
    @param token The token to recover
    """
    assert self.owner == msg.sender, "Access"
    assert not self.token_in_use[token.address] and token.address != self.crvusd_vault.address, "Token not allowed"
    assert extcall token.transfer(msg.sender, staticcall token.balanceOf(self), default_return_value=True)


@external
@view
def assets_for_crvusd(pool_id: uint256, crvusd_amount: uint256) -> uint256:
    """
    @notice Calculate assets amount for a given crvUSD amount
    @dev Uses _required_crvusd_for with debt = assets * price_oracle / 10**18 to compute ratio.
         Accounts for any excess crvUSD already available in the vault.
    @param pool_id The market pool identifier
    @param crvusd_amount The crvUSD amount to deposit
    @return The corresponding assets amount
    """
    market: Market = staticcall FACTORY.markets(pool_id)

    # Account for excess crvusd already available in the vault
    available: uint256 = self._crvusd_available() + crvusd_amount
    required: uint256 = self._downscale(self._required_crvusd())
    effective_crvusd: uint256 = available - min(available, required)

    # Get price from cryptopool's price_oracle
    # test_debt = p_o, test_assets == 1.0
    test_debt: uint256 = staticcall market.cryptopool.price_oracle()

    # Use 1 unit of assets (based on actual decimals) to compute ratio
    # debt = assets * p_o / 1.0 = p_o
    asset_decimals: uint256 = convert(staticcall IERC20Detailed(market.asset_token.address).decimals(), uint256)
    test_assets: uint256 = 10**asset_decimals

    # Get crvusd required for test_assets
    crvusd_for_test: uint256 = self._downscale(self._required_crvusd_for(market, test_assets, test_debt)[1])

    # Scale to get assets for effective_crvusd
    # crvusd_for_test / test_assets = effective_crvusd / assets
    # assets = effective_crvusd * test_assets / crvusd_for_test
    return effective_crvusd * test_assets // crvusd_for_test
