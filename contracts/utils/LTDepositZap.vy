# @version 0.4.3
"""
@title LTDepositZap
@notice Deposit into a Yield Basis market with the debt computed on-chain,
        staked or unstaked
@author Yield Basis
@license MIT, Copyright (c) 2026
@dev A cryptopool policy with an LT gate (YBOraclizedPolicy) admits a deposit
     only at the LevAMM book's ratio:

         debt == added_lp * amm.get_debt() / amm.collateral_amount()

     within its ratio_tolerance (0.01%). A debt quoted off-chain misses it
     whenever the pool trades between quoting and inclusion, so the zap solves
     it in the deposit's own transaction, as the fixed point of

         f(x) = cryptopool.calc_token_amount([x, assets], True) * amm.get_debt() / amm.collateral_amount()

     That debt is fair without a gate too: any other debt trades against the
     market's holders.

     The debt follows the pool's state at inclusion, so min_shares, from
     preview_deposit in the same block, is the sandwich protection.
     No privileges, no state: nothing stays in the zap between transactions.
"""
from ethereum.ercs import IERC20
from ethereum.ercs import IERC20Detailed


interface Cryptopool:
    def price_oracle() -> uint256: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view

interface AMM:
    def get_debt() -> uint256: view
    def collateral_amount() -> uint256: view

interface Gauge:
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable
    def previewDeposit(assets: uint256) -> uint256: view

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address) -> uint256: nonpayable
    def preview_deposit(assets: uint256, debt: uint256, raise_overflow: bool) -> uint256: view
    def approve(_to: address, _amount: uint256) -> bool: nonpayable
    def allowance(_from: address, _to: address) -> uint256: view
    def ASSET_TOKEN() -> address: view
    def CRYPTOPOOL() -> Cryptopool: view
    def amm() -> AMM: view
    def staker() -> Gauge: view


DEBT_STEP: constant(uint256) = 1000  # Finite-difference step for f's slope: 1/1000 of the debt


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
    @dev Two Newton steps from the debt at the pool's price_oracle, with f's
         slope taken once by a finite difference: f' is about 1/2 and nearly
         constant, so each step cuts the error ~50x and the start is a few % off.
    """
    cpool: Cryptopool = staticcall lt.CRYPTOPOOL()
    asset_decimals: uint256 = convert(staticcall IERC20Detailed(staticcall lt.ASSET_TOKEN()).decimals(), uint256)
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


@external
@view
def calc_debt(lt: LT, assets: uint256) -> uint256:
    """
    @notice Debt at the LevAMM book's ratio for depositing `assets` into `lt` now
    @dev Also what a HybridVault deposit needs, though quoted off-chain it
         misses the gate if the pool trades before inclusion
    """
    return self._calc_debt(lt, assets)


@external
@view
def preview_deposit(lt: LT, assets: uint256, stake: bool = False) -> uint256:
    """
    @notice Shares for depositing `assets` into `lt` now: LT shares, or gauge
            shares if staking
    """
    shares: uint256 = staticcall lt.preview_deposit(assets, self._calc_debt(lt, assets), False)
    if stake:
        return staticcall (staticcall lt.staker()).previewDeposit(shares)
    return shares


@external
def deposit(lt: LT, assets: uint256, min_shares: uint256, receiver: address = msg.sender,
            stake: bool = False) -> uint256:
    """
    @notice Deposit `assets` of lt's asset token from the caller
    @param lt Market's LT
    @param assets Amount of the asset token to deposit
    @param min_shares Minimal shares out: LT shares, or gauge shares if staking
    @param receiver Receiver of the shares
    @param stake Stake the LT shares in the market's gauge
    @return Shares received: LT shares, or gauge shares if staking
    """
    asset: IERC20 = IERC20(staticcall lt.ASSET_TOKEN())
    assert extcall asset.transferFrom(msg.sender, self, assets, default_return_value=True)
    if staticcall asset.allowance(self, lt.address) == 0:
        assert extcall asset.approve(lt.address, max_value(uint256), default_return_value=True)

    debt: uint256 = self._calc_debt(lt, assets)

    if not stake:
        return extcall lt.deposit(assets, debt, min_shares, receiver)

    gauge: Gauge = staticcall lt.staker()
    if staticcall lt.allowance(self, gauge.address) == 0:
        extcall lt.approve(gauge.address, max_value(uint256))
    lt_shares: uint256 = extcall lt.deposit(assets, debt, 0, self)
    shares: uint256 = extcall gauge.deposit(lt_shares, receiver)
    assert shares >= min_shares, "Slippage"
    return shares
