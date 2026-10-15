# @version 0.4.3
"""
@title LTMigrator
@notice Migration zap from one version of vault to another
@author Scientia Spectra AG
@license Copyright (c) 2025
"""


interface ERC20:
    def balanceOf(account: address) -> uint256: view
    def transfer(to: address, amount: uint256) -> bool: nonpayable
    def transferFrom(_from: address, to: address, amount: uint256) -> bool: nonpayable
    def approve(spender: address, amount: uint256) -> bool: nonpayable
    def allowance(owner: address, spender: address) -> uint256: view
    def totalSupply() -> uint256: view
    def decimals() -> uint8: view


struct OraclizedValue:
    p_o: uint256
    value: uint256

struct LiquidityValues:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256


struct Market:
    asset_token: ERC20
    cryptopool: Cryptopool
    amm: AMM
    lt: LT
    price_oracle: address
    virtual_pool: address
    staker: Gauge


interface Factory:
    def markets(i: uint256) -> Market: view
    def market_count() -> uint256: view

interface MFOwner:
    def FACTORY() -> Factory: view
    def lt_allocate_stablecoins(lt: LT, limit: uint256): nonpayable
    def lt_in_factory(lt: LT) -> bool: view
    def disabled_lts(lt: address) -> bool: view

interface Cryptopool:
    def balances(i: uint256) -> uint256: view
    def price_oracle() -> uint256: view

interface Gauge:
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable
    def redeem(shares: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def previewDeposit(assets: uint256) -> uint256: view
    def previewRedeem(shares: uint256) -> uint256: view

interface AMM:
    def collect_fees() -> uint256: nonpayable
    def value_oracle() -> OraclizedValue: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address) -> uint256: nonpayable
    def withdraw(shares: uint256, min_assets: uint256) -> uint256: nonpayable
    def balanceOf(user: address) -> uint256: view
    def approve(_to: address, _amount: uint256) -> bool: nonpayable
    def allowance(_from: address, _to: address) -> uint256: view
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable
    def ASSET_TOKEN() -> ERC20: view
    def amm() -> AMM: view
    def allocate_stablecoins(): nonpayable
    def CRYPTOPOOL() -> Cryptopool: view
    def preview_emergency_withdraw(shares: uint256) -> (uint256, int256): view
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def preview_withdraw(tokens: uint256) -> uint256: view
    def staker() -> Gauge: view
    def totalSupply() -> uint256: view
    def liquidity() -> LiquidityValues: view
    def stablecoin_allocation() -> uint256: view


MAX_MARKETS: constant(uint256) = 50000  # Same as in Factory

STABLECOIN: public(immutable(ERC20))
FACTORY_OWNER: public(immutable(MFOwner))


@deploy
def __init__(stablecoin: ERC20, factory_owner: MFOwner):
    STABLECOIN = stablecoin
    FACTORY_OWNER = factory_owner


@internal
@view
def _check_direction(lt_from: LT, lt_to: LT):
    # Forward-only: a newer market supersedes the older markets of the same
    # asset, so lt_to must be the newest market for lt_from's asset (and hence
    # newer than lt_from). Scanning from the newest market down stops at the
    # first one with that asset, so only markets newer than lt_to are read.
    # lt_from can be any factory market, including v1.
    assert lt_from.address != lt_to.address, "same LT"
    assert staticcall FACTORY_OWNER.lt_in_factory(lt_from), "lt_from not in factory"
    assert not (staticcall FACTORY_OWNER.disabled_lts(lt_to.address)), "lt_to deprecated"
    asset: ERC20 = staticcall lt_from.ASSET_TOKEN()
    factory: Factory = staticcall FACTORY_OWNER.FACTORY()
    n: uint256 = staticcall factory.market_count()
    for k: uint256 in range(n, bound=MAX_MARKETS):
        market: Market = staticcall factory.markets(n - 1 - k)
        if market.asset_token == asset:
            assert market.lt == lt_to, "lt_to superseded"
            return
    raise "lt_to superseded"


@internal
@view
def _preview_migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, debt_coefficient: uint256) -> uint256:
    self._check_direction(lt_from, lt_to)

    cpool_from: Cryptopool = staticcall lt_from.CRYPTOPOOL()
    cpool_to: Cryptopool = staticcall lt_to.CRYPTOPOOL()

    assets: uint256 = staticcall lt_from.preview_withdraw(shares_in)

    debt: uint256 = 0
    if cpool_from == cpool_to:
        # Same cryptopool: preserve the position's debt structure.
        cpool_stables: uint256 = staticcall cpool_from.balances(0)
        cpool_assets: uint256 = staticcall cpool_from.balances(1)
        eassets: uint256 = 0
        net_stables: int256 = 0
        eassets, net_stables = staticcall lt_from.preview_emergency_withdraw(shares_in)
        debt = convert(convert(cpool_stables * eassets // cpool_assets, int256) - net_stables, uint256)
    else:
        # Different cryptopool: rebase debt to the new pool's price_oracle so
        # the deposit lands at the AMM's notion of equilibrium (the AMM's
        # coll_value derives from the same oracle), regardless of old-pool
        # drift / interest.
        asset: ERC20 = staticcall lt_from.ASSET_TOKEN()
        asset_decimals: uint256 = convert(staticcall asset.decimals(), uint256)
        debt = assets * (staticcall cpool_to.price_oracle()) // (10**asset_decimals)

    return staticcall lt_to.preview_deposit(assets, debt * debt_coefficient // 10**18, False)


@external
@view
def preview_migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, debt_coefficient: uint256 = 10**18) -> uint256:
    return self._preview_migrate_plain(lt_from, lt_to, shares_in, debt_coefficient)


@external
@view
def preview_migrate_staked(lt_from: LT, lt_to: LT, shares_in: uint256, debt_coefficient: uint256 = 10**18) -> uint256:
    gauge_from: Gauge = staticcall lt_from.staker()
    gauge_to: Gauge = staticcall lt_to.staker()
    lt_in: uint256 = staticcall gauge_from.previewRedeem(shares_in)
    lt_out: uint256 = self._preview_migrate_plain(lt_from, lt_to, lt_in, debt_coefficient)
    return staticcall gauge_to.previewDeposit(lt_out)


@internal
@view
def _required_crvusd_for(lt: LT, amm: AMM, assets: uint256, debt: uint256) -> (uint256, uint256):
    lt_shares: uint256 = staticcall lt.preview_deposit(assets, debt, False)
    lt_supply: uint256 = staticcall lt.totalSupply()
    liquidity: LiquidityValues = staticcall lt.liquidity()
    value_in_amm: uint256 = (staticcall amm.value_oracle()).value
    if lt_supply == 0 or liquidity.total == 0:
        return value_in_amm, 0
    # Holders own total / (total + admin) of the value: admin is accrued on top of total
    admin_balance: uint256 = convert(max(liquidity.admin, 0), uint256)
    return value_in_amm, value_in_amm * liquidity.total // (liquidity.total + admin_balance) * lt_shares // lt_supply


@internal
def _migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, min_out: uint256, debt_coefficient: uint256,
                   _for: address) -> uint256:
    # Check that both LTs are in the factory and lt_to supersedes lt_from
    self._check_direction(lt_from, lt_to)

    # Prepare asset approvals (e.g. WBTC etc)
    asset: ERC20 = staticcall lt_from.ASSET_TOKEN()
    if staticcall asset.allowance(self, lt_to.address) == 0:
        extcall asset.approve(lt_to.address, max_value(uint256))
    amm: AMM = staticcall lt_from.amm()

    # Explicitly collect fees to cryptopool so that they don't screw our measurements
    extcall amm.collect_fees()

    # Withdraw from LT
    debt: uint256 = staticcall STABLECOIN.balanceOf(amm.address)
    assets: uint256 = extcall lt_from.withdraw(shares_in, 0)

    cpool_from: Cryptopool = staticcall lt_from.CRYPTOPOOL()
    cpool_to: Cryptopool = staticcall lt_to.CRYPTOPOOL()
    if cpool_from == cpool_to:
        # Same cryptopool: preserve the position's debt structure.
        debt = (staticcall STABLECOIN.balanceOf(amm.address)) - debt
    else:
        # Different cryptopool: rebase debt to the new pool's price_oracle so
        # the deposit lands at the AMM's notion of equilibrium (the AMM's
        # coll_value derives from the same oracle), regardless of old-pool
        # drift / interest. The freed crvUSD stays in the old AMM and is
        # harvested by the subsequent deallocation if lt_from is disabled.
        asset_decimals: uint256 = convert(staticcall asset.decimals(), uint256)
        debt = assets * (staticcall cpool_to.price_oracle()) // (10**asset_decimals)

    debt = debt * debt_coefficient // 10**18

    pool_value: uint256 = 0
    additional_crvusd: uint256 = 0
    pool_value, additional_crvusd = self._required_crvusd_for(lt_to, staticcall lt_to.amm(), assets, debt)

    # Save previous allocation
    previous_allocation: uint256 = staticcall lt_to.stablecoin_allocation()

    # Now we freed up some stablecoins in the AMM. Harvest them from a disabled
    # lt_from (best-effort: anyone can retry on HybridFactoryOwner).
    if staticcall FACTORY_OWNER.disabled_lts(lt_from.address):
        success: bool = raw_call(
            FACTORY_OWNER.address,
            abi_encode(lt_from.address, empty(uint256), method_id=method_id("lt_allocate_stablecoins(address,uint256)")),
            revert_on_failure=False)

    # Allocate more
    extcall FACTORY_OWNER.lt_allocate_stablecoins(lt_to, max((pool_value + additional_crvusd) * 22 // 10, previous_allocation))

    shares: uint256 = extcall lt_to.deposit(assets, debt, min_out, _for)

    extcall FACTORY_OWNER.lt_allocate_stablecoins(lt_to, previous_allocation)

    return shares


@external
def migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, min_out: uint256,
                  debt_coefficient: uint256 = 10**18):
    extcall lt_from.transferFrom(msg.sender, self, shares_in)
    self._migrate_plain(lt_from, lt_to, shares_in, min_out, debt_coefficient, msg.sender)


@external
def migrate_staked(lt_from: LT, lt_to: LT, shares_in: uint256, min_out: uint256,
                   debt_coefficient: uint256 = 10**18):
    gauge_from: Gauge = staticcall lt_from.staker()
    gauge_to: Gauge = staticcall lt_to.staker()

    if staticcall lt_to.allowance(self, gauge_to.address) == 0:
        extcall lt_to.approve(gauge_to.address, max_value(uint256))

    lt_in: uint256 = extcall gauge_from.redeem(shares_in, self, msg.sender)
    lt_out: uint256 = self._migrate_plain(lt_from, lt_to, lt_in, 0, debt_coefficient, self)
    shares_out: uint256 = extcall gauge_to.deposit(lt_out, msg.sender)
    assert shares_out >= min_out, "not enough out"
