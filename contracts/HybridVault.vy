# @version 0.4.3
"""
@title HybridVaultFactory
@notice Factory for vaults which keep both YB vaults and scrvUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
"""

interface Factory:
    def admin() -> address: view


FACTORY: public(immutable(Factory))
user: public(address)


@deploy
def __init__(factory: Factory):
    self.user = 0x0000000000000000000000000000000000000001  # To prevent initializing the factory itself
    FACTORY = factory

@external
def initialize(user: address) -> bool:
    assert self.user == empty(address), "Already initialized"
    self.user = user
    return True
