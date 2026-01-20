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


FACTORY: public(immutable(Factory))
ADMIN: public(immutable(address))
vault_impl: public(address)
user_to_vault: public(HashMap[address, HybridVault])
vault_to_user: public(HashMap[HybridVault, address])
stablecoin_fraction: public(uint256)
pool_limits: public(HashMap[uint256, uint256])
allowed_crvusd_vaults: public(HashMap[address, bool])


@deploy
def __init__(factory: Factory, impl: address, pool_ids: DynArray[uint256, 10], pool_limits: DynArray[uint256, 10]):
    """
    @notice Initialize the HybridVaultFactory with configuration parameters
    @param factory The main Yield Basis factory contract
    @param impl The initial vault implementation address for minimal proxies
    @param pool_ids Array of pool identifiers to configure limits for
    @param pool_limits Array of deposit limits corresponding to each pool_id
    """
    FACTORY = factory
    ADMIN = staticcall (staticcall factory.admin()).ADMIN()
    self.vault_impl = impl
    self.stablecoin_fraction = 4 * 10**17
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
def set_allowed_crvusd_vault(vault: address, allowed: bool):
    """
    @notice Enable or disable a crvUSD vault for use
    @dev Only callable by ADMIN
    @param vault The crvUSD vault address
    @param allowed Whether the vault is allowed
    """
    assert msg.sender == ADMIN, "Access"
    self.allowed_crvusd_vaults[vault] = allowed
    log SetAllowedCrvusdVault(vault=vault, allowed=allowed)


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
