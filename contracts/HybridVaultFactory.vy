# @version 0.4.3
"""
@title HybridVaultFactory
@notice Factory for vaults which keep both YB vaults and scrvUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
"""

interface HybridVault:
    def initialize(user: address, crvusd_vault: address) -> bool: nonpayable

interface FactoryOwner:
    def ADMIN() -> address: view
    def lt_allocate_stablecoins(lt: address, limit: uint256): nonpayable

interface Factory:
    def admin() -> FactoryOwner: view


event VaultCreated:
    user: indexed(address)
    vault: indexed(HybridVault)

event SetVaultImpl:
    impl: address

event SetStablecoinFraction:
    stablecoin_fraction: uint256

event SetPoolLimit:
    pool_id: uint256
    limit: uint256

event SetAllowedCrvusdVault:
    vault: address
    allowed: bool

event SetCrvusdVaultLimit:
    crvusd_vault: address
    limit: uint256


FACTORY: public(immutable(Factory))
ADMIN: public(immutable(address))
vault_impl: public(address)
user_to_vault: public(HashMap[address, HybridVault])
vault_to_user: public(HashMap[HybridVault, address])
stablecoin_fraction: public(uint256)
pool_limits: public(HashMap[uint256, uint256])
allowed_crvusd_vaults: public(HashMap[address, bool])
crvusd_vault_limits: public(HashMap[address, uint256])
crvusd_vault_total_required: public(HashMap[address, uint256])
crvusd_vault_required: public(HashMap[HybridVault, uint256])


@deploy
def __init__(factory: Factory, pool_ids: DynArray[uint256, 10], pool_limits: DynArray[uint256, 10]):
    """
    @notice Initialize the HybridVaultFactory with configuration parameters
    @dev vault_impl must be set via set_vault_impl before creating vaults
    @param factory The main Yield Basis factory contract
    @param pool_ids Array of pool identifiers to configure limits for
    @param pool_limits Array of deposit limits corresponding to each pool_id
    """
    FACTORY = factory
    ADMIN = staticcall (staticcall factory.admin()).ADMIN()
    self.stablecoin_fraction = 55 * 10**16
    for i: uint256 in range(10):
        if i >= len(pool_ids):
            break
        pool_id: uint256 = pool_ids[i]
        pool_limit: uint256 = pool_limits[i]
        self.pool_limits[pool_id] = pool_limit
        log SetPoolLimit(pool_id=pool_id, limit=pool_limit)


@external
def create_vault(crvusd_vault: address) -> HybridVault:
    """
    @notice Create a new HybridVault for the caller
    @dev Deploys a minimal proxy pointing to vault_impl and initializes it.
         Each address can only create one vault.
    @param crvusd_vault The crvUSD vault (e.g., scrvUSD) to use for this vault
    @return The newly created HybridVault instance
    """
    assert self.user_to_vault[msg.sender] == empty(HybridVault), "Already created"
    assert self.allowed_crvusd_vaults[crvusd_vault], "Vault not allowed"
    assert self.vault_impl != empty(address), "Vault impl not set"

    vault: HybridVault = HybridVault(create_minimal_proxy_to(self.vault_impl))
    extcall vault.initialize(msg.sender, crvusd_vault)

    self.user_to_vault[msg.sender] = vault
    self.vault_to_user[vault] = msg.sender

    log VaultCreated(user=msg.sender, vault=vault)
    return vault


@external
def set_vault_impl(impl: address):
    """
    @notice Update the vault implementation used for new vault deployments
    @dev Only callable by ADMIN. Does not affect existing vaults.
    @param impl The new vault implementation address
    """
    assert msg.sender == ADMIN, "Access"
    self.vault_impl = impl
    log SetVaultImpl(impl=impl)


@external
def set_stablecoin_fraction(frac: uint256):
    """
    @notice Set the target fraction of deposits to be held as stablecoins (scrvUSD)
    @dev Only callable by ADMIN. Value is in 18-decimal precision (e.g., 4e17 = 40%)
    @param frac The stablecoin fraction as a fraction of 1e18
    """
    assert msg.sender == ADMIN, "Access"
    self.stablecoin_fraction = frac
    log SetStablecoinFraction(stablecoin_fraction=frac)


@external
def set_pool_limit(pool_id: uint256, pool_limit: uint256):
    """
    @notice Set the deposit limit for a specific pool
    @dev Only callable by ADMIN
    @param pool_id The identifier of the pool
    @param pool_limit The maximum deposit amount allowed for the pool
    """
    assert msg.sender == ADMIN, "Access"
    self.pool_limits[pool_id] = pool_limit
    log SetPoolLimit(pool_id=pool_id, limit=pool_limit)


@external
def set_allowed_crvusd_vault(vault: address, allowed: bool, limit: uint256 = 0):
    """
    @notice Enable or disable a crvUSD vault for use, with an optional deposit limit
    @dev Only callable by ADMIN. Limit of 0 means unlimited.
    @param vault The crvUSD vault address
    @param allowed Whether the vault is allowed
    @param limit The maximum total required crvUSD across all HybridVaults using this vault
    """
    assert msg.sender == ADMIN, "Access"
    self.allowed_crvusd_vaults[vault] = allowed
    self.crvusd_vault_limits[vault] = limit
    log SetAllowedCrvusdVault(vault=vault, allowed=allowed)
    log SetCrvusdVaultLimit(crvusd_vault=vault, limit=limit)


@external
def set_crvusd_vault_limit(crvusd_vault: address, limit: uint256):
    """
    @notice Set the total deposit limit for a crvUSD vault across all HybridVaults
    @dev Only callable by ADMIN. 0 means no limit.
    @param crvusd_vault The crvUSD vault address
    @param limit The maximum total required crvUSD allowed across all HybridVaults using this vault
    """
    assert msg.sender == ADMIN, "Access"
    self.crvusd_vault_limits[crvusd_vault] = limit
    log SetCrvusdVaultLimit(crvusd_vault=crvusd_vault, limit=limit)


@external
def update_vault_required(crvusd_vault: address, new_required: uint256, check_limit: bool = True):
    """
    @notice Update the tracked required crvUSD for a HybridVault
    @dev Only callable by registered HybridVaults. Reverts on increase if vault limit exceeded and check_limit is True.
    @param crvusd_vault The crvUSD vault address the HybridVault uses
    @param new_required The new total required crvUSD for this HybridVault
    @param check_limit If True, revert when increase would exceed the vault limit
    """
    hybrid_vault: HybridVault = HybridVault(msg.sender)
    assert self.vault_to_user[hybrid_vault] != empty(address), "Only vaults can call"

    old_required: uint256 = self.crvusd_vault_required[hybrid_vault]

    if new_required > old_required:
        increase: uint256 = new_required - old_required
        total: uint256 = self.crvusd_vault_total_required[crvusd_vault] + increase
        if check_limit:
            vault_limit: uint256 = self.crvusd_vault_limits[crvusd_vault]
            if vault_limit > 0:
                assert total <= vault_limit, "Beyond vault limit"
        self.crvusd_vault_total_required[crvusd_vault] = total
    elif new_required < old_required:
        decrease: uint256 = old_required - new_required
        self.crvusd_vault_total_required[crvusd_vault] -= decrease

    self.crvusd_vault_required[hybrid_vault] = new_required


@external
def lt_allocate_stablecoins(lt: address, limit: uint256):
    """
    @notice Allocate stablecoins to a liquidity token via the factory admin
    @dev Only callable by registered vaults. Forwards the call to FactoryOwner.
    @param lt The liquidity token address to allocate stablecoins for
    @param limit The allocation limit
    """
    assert self.vault_to_user[HybridVault(msg.sender)] != empty(address), "Only vaults can call"
    extcall (staticcall FACTORY.admin()).lt_allocate_stablecoins(lt, limit)
