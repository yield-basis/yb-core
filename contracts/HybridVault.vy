# @version 0.4.3
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

interface CurveCryptoPool:
    def price_scale() -> uint256: view

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address) -> uint256: nonpayable
    def agg() -> PriceOracle: view
    def balanceOf(user: address) -> uint256: view
    def totalSupply() -> uint256: view
    def liquidity() -> LiquidityValues: view

interface AMM:
    def max_debt() -> uint256: view
    def get_debt() -> uint256: view
    def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> OraclizedValue: view
    def value_oracle() -> uint256: view

interface GaugeController:
    def is_killed(gauge: address) -> bool: view

interface Factory:
    def admin() -> address: view
    def gauge_controller() -> GaugeController: view
    def markets(idx: uint256) -> Market: view

interface VaultFactory:
    def stablecoin_fraction() -> uint256: view
    def pool_limits(pool_id: uint256) -> uint256: view


MAX_VAULTS: public(constant(uint256)) = 16
FACTORY: public(immutable(Factory))
GC: public(immutable(GaugeController))
CRVUSD: public(immutable(IERC20))
CRVUSD_VAULT: public(immutable(IERC4626))
owner: public(address)
vault_factory: public(VaultFactory)
used_vaults: public(DynArray[uint256, MAX_VAULTS])


@deploy
def __init__(factory: Factory, crvusd: IERC20, crvusd_vault: IERC4626):
    # XXX add factory owner also
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
def _crvusd_available() -> uint256:
    return staticcall CRVUSD_VAULT.previewRedeem(staticcall CRVUSD_VAULT.balanceOf(self))


@internal
@view
def _required_crvusd() -> uint256:
    total_crvusd: uint256 = 0
    for pool_id: uint256 in self.used_vaults:
        pool: Market = staticcall FACTORY.markets(pool_id)
        lt_shares: uint256 = staticcall pool.lt.balanceOf(self) + staticcall pool.staker.previewRedeem(staticcall pool.staker.balanceOf(self))
        lt_total: uint256 = staticcall pool.lt.totalSupply()
        liquidity: LiquidityValues = staticcall pool.lt.liquidity()
        crvusd_amount: uint256 = staticcall pool.amm.value_oracle()
        crvusd_amount = crvusd_amount * (liquidity.total - convert(max(liquidity.admin, 0), uint256)) // liquidity.total * lt_shares // lt_total
        total_crvusd += crvusd_amount
    return total_crvusd * (staticcall self.vault_factory.stablecoin_fraction()) // 10**18


@internal
@view
def _required_crvusd_for(amm: AMM, collateral_amount: uint256, borrowed_amount: uint256) -> uint256:
    value_before: uint256 = (staticcall amm.value_change(0, 0, True)).value
    value_after: uint256 = (staticcall amm.value_change(collateral_amount, borrowed_amount, True)).value
    return (value_after - value_before) * (staticcall self.vault_factory.stablecoin_fraction()) // 10**18


@external
@view
def required_crvusd() -> uint256:
    return self._required_crvusd()


@external
@view
def required_crvusd_for(pool_id: uint256, collateral_amount: uint256, borrowed_amount: uint256) -> uint256:
    return self._required_crvusd_for((staticcall FACTORY.markets(pool_id)).amm, collateral_amount, borrowed_amount)


@external
def deposit(pool_id: uint256, assets: uint256, debt: uint256, min_shares: uint256, stake: bool = False, receiver: address = msg.sender) -> uint256:
    # When deposit: raise cap, deposit, reduce cap
    return 0


@external
def withdraw(pool_id: uint256, shares: uint256, min_assets: uint256, unstake: bool = False, receiver: address = msg.sender) -> uint256:
    return 0


@external
def claim_rewards():
    pass


@external
def deposit_crvusd(assets: uint256) -> uint256:
    extcall CRVUSD.transferFrom(msg.sender, self, assets)
    return extcall CRVUSD_VAULT.deposit(assets, self)


@external
def redeem_crvusd(shares: uint256) -> uint256:
    assert self.owner == msg.sender, "Access"
    withdrawn: uint256 = extcall CRVUSD_VAULT.redeem(shares, msg.sender, self)
    assert self._crvusd_available() >= self._required_crvusd(), "Not enough crvUSD left"
    return withdrawn


@external
def deposit_scrvusd(shares: uint256):
    extcall CRVUSD_VAULT.transferFrom(msg.sender, self, shares)


@external
def withdraw_scrvusd(shares: uint256):
    assert self.owner == msg.sender, "Access"
    extcall CRVUSD_VAULT.transfer(msg.sender, shares)
    assert self._crvusd_available() >= self._required_crvusd(), "Not enough crvUSD left"
