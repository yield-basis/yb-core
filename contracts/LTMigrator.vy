# @version 0.4.3
"""
@title LTMigrator
@notice Migration zap from one version of vault to another
@author Scientia Spectra AG
@license Copyright (c) 2025
"""
from ethereum.ercs import IERC20


interface MFOwner:
    def lt_allocate_stablecoins(lt: LT, limit: uint256): nonpayable

interface Cryptopool:
    def balances(i: uint256) -> uint256: view

interface Gauge:
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable
    def redeem(shares: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def previewDeposit(assets: uint256) -> uint256: view
    def previewRedeem(shares: uint256) -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address) -> uint256: nonpayable
    def withdraw(shares: uint256, min_assets: uint256) -> uint256: nonpayable
    def balanceOf(user: address) -> uint256: view
    def approve(_to: address, _amount: uint256) -> bool: nonpayable
    def allowance(_from: address, _to: address) -> uint256: view
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable
    def ASSET_TOKEN() -> IERC20: view
    def amm() -> address: view
    def allocate_stablecoins(): nonpayable
    def CRYPTOPOOL() -> Cryptopool: view
    def preview_emergency_withdraw(shares: uint256) -> (uint256, int256): view
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def preview_withdraw(tokens: uint256) -> uint256: view
    def staker() -> Gauge: view


STABLECOIN: public(immutable(IERC20))
FACTORY_OWNER: public(immutable(MFOwner))


@deploy
def __init__(stablecoin: IERC20, factory_owner: MFOwner):
    STABLECOIN = stablecoin
    FACTORY_OWNER = factory_owner


@internal
@view
def _preview_migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, debt_coefficient: uint256) -> uint256:
    cpool: Cryptopool = staticcall lt_from.CRYPTOPOOL()
    cpool_stables: uint256 = staticcall cpool.balances(0)
    cpool_assets: uint256 = staticcall cpool.balances(1)

    eassets: uint256 = (staticcall lt_from.preview_emergency_withdraw(shares_in))[0]
    debt: uint256 = cpool_stables * eassets // cpool_assets

    assets: uint256 = staticcall lt_from.preview_withdraw(shares_in)

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
def _migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, min_out: uint256, debt_coefficient: uint256,
                   _for: address) -> uint256:
    # Prepare asset approvals (e.g. WBTC etc)
    asset: IERC20 = staticcall lt_from.ASSET_TOKEN()
    if staticcall asset.allowance(self, lt_to.address) == 0:
        extcall asset.approve(lt_to.address, max_value(uint256))
    amm: address = staticcall lt_from.amm()

    # Withdraw from LT
    debt: uint256 = staticcall STABLECOIN.balanceOf(amm)
    assets: uint256 = extcall lt_from.withdraw(shares_in, 0)
    debt = (staticcall STABLECOIN.balanceOf(amm)) - debt

    # Now we freed up some stablecoins in the AMM
    extcall FACTORY_OWNER.lt_allocate_stablecoins(lt_from, 0)  # Take what freed up from old allocation
    extcall lt_to.allocate_stablecoins()  # Give these coins to the new AMM

    debt = debt * debt_coefficient // 10**18

    return extcall lt_to.deposit(assets, debt, min_out, _for)


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
