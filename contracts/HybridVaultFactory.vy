# @version 0.4.3
"""
@title HybridVaultFactory
@notice Factory for vaults which keep both YB vaults and scrvUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
"""

interface HybridVault:
    def initialize(user: address) -> bool: nonpayable

interface Factory:
    def admin() -> address: view


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


FACTORY: public(immutable(Factory))
vault_impl: public(address)
user_to_vault: public(HashMap[address, HybridVault])
vault_to_user: public(HashMap[HybridVault, address])
stablecoin_fraction: public(uint256)
pool_limits: public(HashMap[uint256, uint256])


@deploy
def __init__(factory: Factory, impl: address, pool_ids: DynArray[uint256, 10], pool_limits: DynArray[uint256, 10]):
    FACTORY = factory
    self.vault_impl = impl
    self.stablecoin_fraction = 4 * 10**17
    for i: uint256 in range(10):
        if i > len(pool_ids):
            break
        pool_id: uint256 = pool_ids[i]
        pool_limit: uint256 = pool_limits[i]
        self.pool_limits[pool_id] = pool_limit
        log SetPoolLimit(pool_id=pool_id, limit=pool_limit)


@external
def create_vault() -> HybridVault:
    assert self.user_to_vault[msg.sender] == empty(HybridVault), "Already created"

    vault: HybridVault = HybridVault(create_minimal_proxy_to(self.vault_impl))
    extcall vault.initialize(msg.sender)

    self.user_to_vault[msg.sender] = vault
    self.vault_to_user[vault] = msg.sender

    log VaultCreated(user=msg.sender, vault=vault)
    return vault


@external
def set_vault_impl(impl: address):
    assert msg.sender == staticcall FACTORY.admin(), "Access"
    self.vault_impl = impl
    log SetVaultImpl(impl=impl)


@external
def set_stablecoin_fraction(frac: uint256):
    assert msg.sender == staticcall FACTORY.admin(), "Access"
    self.stablecoin_fraction = frac
    log SetStablecoinFraction(stablecoin_fraction=frac)


@external
def set_pool_limit(pool_id: uint256, pool_limit: uint256):
    assert msg.sender == staticcall FACTORY.admin(), "Access"
    self.pool_limits[pool_id] = pool_limit
    log SetPoolLimit(pool_id=pool_id, limit=pool_limit)
