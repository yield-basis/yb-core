# @version 0.4.3
"""
@title LTMigratorGated
@notice Migration zap from one version of vault to another, for target markets
        whose cryptopool policy gates LT deposits (and any other market)
@author Yield Basis
@license GNU Affero General Public License v3.0
@dev Same ABI as LTMigrator. The pool policy (YBOraclizedPolicy) admits an LT
     deposit only at the LevAMM book's ratio:

         debt == added_lp * amm.get_debt() / amm.collateral_amount()

     within its ratio_tolerance (0.01%). The LP minted depends on the debt
     itself, so the debt is the fixed point of

         f(x) = cryptopool.calc_token_amount([x, assets], True) * amm.get_debt() / amm.collateral_amount()

     solved on-chain right before the deposit, as in LTDepositZap. That debt
     is also the fair one without a gate: any other debt trades against
     lt_to's holders. So debt_coefficient is accepted for ABI compatibility
     and ignored.

     Sandwiches. The gate pins the deposit to the LevAMM book's ratio: a
     front-run in the cryptopool cannot move the debt off it, and the deposit
     then trades against the attacker. In the LevAMM the deposit is
     proportional, so a LevAMM round trip around it gains from the curve's
     convexity, though far less than the LevAMM fee costs at any sane
     min_out. min_out, from preview_migrate_* in the same block, bounds the
     loss, as it does for the source market's withdrawal.
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
    def price_oracle() -> uint256: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view

interface Gauge:
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable
    def redeem(shares: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def previewDeposit(assets: uint256) -> uint256: view
    def previewRedeem(shares: uint256) -> uint256: view

interface AMM:
    def value_oracle() -> OraclizedValue: view
    def get_debt() -> uint256: view
    def collateral_amount() -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address) -> uint256: nonpayable
    def withdraw(shares: uint256, min_assets: uint256) -> uint256: nonpayable
    def balanceOf(user: address) -> uint256: view
    def approve(_to: address, _amount: uint256) -> bool: nonpayable
    def allowance(_from: address, _to: address) -> uint256: view
    def transferFrom(_from: address, _to: address, _amount: uint256) -> bool: nonpayable
    def ASSET_TOKEN() -> ERC20: view
    def amm() -> AMM: view
    def CRYPTOPOOL() -> Cryptopool: view
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def preview_withdraw(tokens: uint256) -> uint256: view
    def staker() -> Gauge: view
    def totalSupply() -> uint256: view
    def liquidity() -> LiquidityValues: view
    def stablecoin_allocation() -> uint256: view


MAX_MARKETS: constant(uint256) = 50000  # Same as in Factory
DEBT_STEP: constant(uint256) = 1000  # Finite-difference step for f's slope: 1/1000 of the debt

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
@pure
def _newton_step(x: uint256, fx: uint256, step: uint256, k: uint256) -> uint256:
    # x + (f(x) - x) / (1 - f'), with step * (1 - f') = k
    if fx > x:
        return x + (fx - x) * step // k
    return x - (x - fx) * step // k


@internal
@view
def _calc_debt(lt: LT, assets: uint256) -> uint256:
    """
    @dev Same as LTDepositZap: two Newton steps from the debt at the pool's
         price_oracle, with f's slope taken once by a finite difference: f' is
         about 1/2 and nearly constant, so each step cuts the error ~50x and
         the start is a few % off.
    """
    cpool: Cryptopool = staticcall lt.CRYPTOPOOL()
    asset_decimals: uint256 = convert(staticcall (staticcall lt.ASSET_TOKEN()).decimals(), uint256)
    d0: uint256 = assets * (staticcall cpool.price_oracle()) // 10**asset_decimals

    amm: AMM = staticcall lt.amm()
    book_collateral: uint256 = staticcall amm.collateral_amount()
    if book_collateral == 0:
        return d0  # An empty book has no ratio to keep
    book_debt: uint256 = staticcall amm.get_debt()

    f0: uint256 = staticcall cpool.calc_token_amount([d0, assets], True) * book_debt // book_collateral
    step: uint256 = d0 // DEBT_STEP + 1
    k: uint256 = step + f0 - staticcall cpool.calc_token_amount([d0 + step, assets], True) * book_debt // book_collateral
    d1: uint256 = self._newton_step(d0, f0, step, k)
    f1: uint256 = staticcall cpool.calc_token_amount([d1, assets], True) * book_debt // book_collateral
    return self._newton_step(d1, f1, step, k)


@internal
@view
def _preview_migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256) -> uint256:
    self._check_direction(lt_from, lt_to)
    assets: uint256 = staticcall lt_from.preview_withdraw(shares_in)
    # Exact when the two markets have different cryptopools: the withdrawal
    # leaves lt_to's pool as it is
    return staticcall lt_to.preview_deposit(assets, self._calc_debt(lt_to, assets), False)


@external
@view
def preview_migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, debt_coefficient: uint256 = 10**18) -> uint256:
    """
    @param debt_coefficient Ignored, kept for LTMigrator's ABI
    """
    return self._preview_migrate_plain(lt_from, lt_to, shares_in)


@external
@view
def preview_migrate_staked(lt_from: LT, lt_to: LT, shares_in: uint256, debt_coefficient: uint256 = 10**18) -> uint256:
    """
    @param debt_coefficient Ignored, kept for LTMigrator's ABI
    """
    gauge_from: Gauge = staticcall lt_from.staker()
    gauge_to: Gauge = staticcall lt_to.staker()
    lt_in: uint256 = staticcall gauge_from.previewRedeem(shares_in)
    lt_out: uint256 = self._preview_migrate_plain(lt_from, lt_to, lt_in)
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
def _migrate_plain(lt_from: LT, lt_to: LT, shares_in: uint256, min_out: uint256, _for: address) -> uint256:
    # Check that both LTs are in the factory and lt_to supersedes lt_from
    self._check_direction(lt_from, lt_to)

    # Prepare asset approvals (e.g. WBTC etc)
    asset: ERC20 = staticcall lt_from.ASSET_TOKEN()
    if staticcall asset.allowance(self, lt_to.address) == 0:
        extcall asset.approve(lt_to.address, max_value(uint256))

    # Withdraw from LT. The freed crvUSD stays in the old AMM and is harvested
    # by the deallocation below if lt_from is disabled.
    assets: uint256 = extcall lt_from.withdraw(shares_in, 0)

    # Solved after the withdrawal, in case it shares lt_to's cryptopool. Nothing
    # else touches that pool or lt_to's LevAMM book before the deposit.
    debt: uint256 = self._calc_debt(lt_to, assets)

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
    """
    @param debt_coefficient Ignored, kept for LTMigrator's ABI
    """
    extcall lt_from.transferFrom(msg.sender, self, shares_in)
    self._migrate_plain(lt_from, lt_to, shares_in, min_out, msg.sender)


@external
def migrate_staked(lt_from: LT, lt_to: LT, shares_in: uint256, min_out: uint256,
                   debt_coefficient: uint256 = 10**18):
    """
    @param debt_coefficient Ignored, kept for LTMigrator's ABI
    """
    gauge_from: Gauge = staticcall lt_from.staker()
    gauge_to: Gauge = staticcall lt_to.staker()

    if staticcall lt_to.allowance(self, gauge_to.address) == 0:
        extcall lt_to.approve(gauge_to.address, max_value(uint256))

    lt_in: uint256 = extcall gauge_from.redeem(shares_in, self, msg.sender)
    lt_out: uint256 = self._migrate_plain(lt_from, lt_to, lt_in, 0, self)
    shares_out: uint256 = extcall gauge_to.deposit(lt_out, msg.sender)
    assert shares_out >= min_out, "not enough out"
