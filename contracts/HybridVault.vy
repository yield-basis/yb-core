# @version 0.4.3
"""
@title HybridVaultFactory
@notice Factory for vaults which keep both YB vaults and scrvUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
"""
from ethereum.ercs import IERC20
from ethereum.ercs import IERC4626


interface Factory:
    def admin() -> address: view


FACTORY: public(immutable(Factory))
CRVUSD: public(immutable(IERC20))
CRVUSD_VAULT: public(immutable(IERC4626))
user: public(address)


@deploy
def __init__(factory: Factory, crvusd: IERC20, crvusd_vault: IERC4626):
    self.user = 0x0000000000000000000000000000000000000001  # To prevent initializing the factory itself
    FACTORY = factory
    CRVUSD = crvusd
    CRVUSD_VAULT = crvusd_vault


@external
def initialize(user: address) -> bool:
    assert self.user == empty(address), "Already initialized"
    self.user = user
    extcall CRVUSD.approve(CRVUSD_VAULT.address, max_value(uint256))
    return True
