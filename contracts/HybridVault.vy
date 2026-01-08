# @version 0.4.3
"""
@title HybridVaultFactory
@notice Factory for vaults which keep both YB vaults and scrvUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
"""
from ethereum.ercs import IERC20
from ethereum.ercs import IERC4626


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


interface CurveCryptoPool:
    def price_scale() -> uint256: view

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address) -> uint256: nonpayable
    def agg() -> PriceOracle: view

interface AMM:
    def max_debt() -> uint256: view
    def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> OraclizedValue: view

interface GaugeController:
    def is_killed(gauge: address) -> bool: view

interface Factory:
    def admin() -> address: view
    def gauge_controller() -> GaugeController: view
    def markets(idx: uint256) -> Market: view

interface VaultFactory:
    def stablecoin_fraction() -> uint256: view


FACTORY: public(immutable(Factory))
GC: public(immutable(GaugeController))
CRVUSD: public(immutable(IERC20))
CRVUSD_VAULT: public(immutable(IERC4626))
owner: public(address)
vault_factory: public(VaultFactory)


@deploy
def __init__(factory: Factory, crvusd: IERC20, crvusd_vault: IERC4626):
    self.owner = 0x0000000000000000000000000000000000000001  # To prevent initializing the factory itself
    FACTORY = factory
    GC = staticcall factory.gauge_controller()
    CRVUSD = crvusd
    CRVUSD_VAULT = crvusd_vault


@external
def initialize(user: address) -> bool:
    assert self.owner == empty(address), "Already initialized"
    self.owner = user
    self.vault_factory = VaultFactory(msg.sender)
    extcall CRVUSD.approve(CRVUSD_VAULT.address, max_value(uint256))
    return True


@internal
def _pool(pool_id: uint256, fail_on_kill: bool) -> Market:
    market: Market = staticcall FACTORY.markets(pool_id)
    if fail_on_kill:
        assert not staticcall GC.is_killed(market.staker.address), "Gauge is killed"
    return market
