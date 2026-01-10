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
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def agg() -> PriceOracle: view
    def balanceOf(user: address) -> uint256: view
    def approve(_for: address, amount: uint256) -> bool: nonpayable
    def totalSupply() -> uint256: view
    def liquidity() -> LiquidityValues: view

interface AMM:
    def max_debt() -> uint256: view
    def get_debt() -> uint256: view
    def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> OraclizedValue: view
    def value_oracle() -> OraclizedValue: view

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

pool_approved: HashMap[uint256, bool]


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
        crvusd_amount: uint256 = (staticcall pool.amm.value_oracle()).value
        crvusd_amount = crvusd_amount * (liquidity.total - convert(max(liquidity.admin, 0), uint256)) // liquidity.total * lt_shares // lt_total
        total_crvusd += crvusd_amount
    return total_crvusd * (staticcall self.vault_factory.stablecoin_fraction()) // 10**18


@internal
@view
def _required_crvusd_for(lt: LT, amm: AMM, assets: uint256, debt: uint256) -> uint256:
    # Only works when lt_supply > 0
    # Also probably make ceil div?
    lt_shares: uint256 = staticcall lt.preview_deposit(assets, debt, False)
    lt_supply: uint256 = staticcall lt.totalSupply()
    value_in_amm: uint256 = (staticcall amm.value_oracle()).value
    return value_in_amm * lt_shares // lt_supply * (staticcall self.vault_factory.stablecoin_fraction()) // 10**18


@external
@view
def required_crvusd() -> uint256:
    return self._required_crvusd()


@external
@view
def required_crvusd_for(pool_id: uint256, assets: uint256, debt: uint256) -> uint256:
    market: Market = staticcall FACTORY.markets(pool_id)
    return self._required_crvusd_for(market.lt, market.amm, assets, debt)


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


@external
def deposit(pool_id: uint256, assets: uint256, debt: uint256, min_shares: uint256, stake: bool = False, receiver: address = msg.sender) -> uint256:
    assert self.owner == msg.sender, "Access"
    market: Market = staticcall FACTORY.markets(pool_id)
    if not self.pool_approved[pool_id]:
        assert extcall market.asset_token.approve(market.lt.address, max_value(uint256), default_return_value=True)
        extcall market.lt.approve(market.staker.address, max_value(uint256))
        self.pool_approved[pool_id] = True

    assert self._crvusd_available() >= self._required_crvusd() + self._required_crvusd_for(market.lt, market.amm, assets, debt), "Not enough crvUSD"

    # XXX increase cap

    lt_receiver: address = receiver
    if stake:
        lt_receiver = self

    if assets > 0:
        self._add_to_used(pool_id)

    assert extcall market.asset_token.transferFrom(msg.sender, self, assets, default_return_value=True)
    lt_shares: uint256 = extcall market.lt.deposit(assets, debt, min_shares, lt_receiver)
    #
    # XXX reduce cap

    if not stake:
        return lt_shares

    else:
        return extcall market.staker.deposit(lt_shares, receiver)


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
