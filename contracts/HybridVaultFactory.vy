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


FACTORY: public(immutable(Factory))
vault_impl: public(address)
user_to_vault: public(HashMap[address, HybridVault])
vault_to_user: public(HashMap[HybridVault, address])


@deploy
def __init__(factory: Factory, impl: address):
    FACTORY = factory
    self.vault_impl = impl


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
