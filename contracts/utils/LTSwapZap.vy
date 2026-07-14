# @version 0.4.3
"""
@title LTSwapZap
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Permissionless best-effort converter: pulls a caller's shares of one YB LT (via
        transferFrom, so the caller must approve this zap first), withdraws them to the pool
        asset, and swaps that to crvUSD with on-chain, oracle-bounded slippage protection (the
        same two legs as PID._convert_fees), sending the crvUSD to the caller. If the swap
        can't meet its min (thin/off-peg pool) the swap error is swallowed and the withdrawn
        asset is handed to the caller instead. Convert several LTs by calling once per LT.
@dev The min_assets/min_dy are computed at execution from the manipulation-resistant
     YBNetPressure half-TVL and the cryptopool price_oracle, so the swap is protected without a
     trusted off-chain slippage bound. The zap pulls min(caller balance, caller allowance), so
     the approval caps the amount. The owner (DAO) only tunes the slippage multiplier and can
     sweep stuck tokens - no special role in conversion.
"""
from ethereum.ercs import IERC20
from snekmate.auth import ownable


initializes: ownable
exports: (ownable.owner, ownable.transfer_ownership)


# Mirrors YBNetPressure.PressureTvl.
struct PressureTvl:
    net_pressure: int256
    half_tvl: uint256


interface CryptoPool:
    def exchange(i: uint256, j: uint256, dx: uint256, min_dy: uint256, receiver: address) -> uint256: nonpayable
    def price_oracle() -> uint256: view
    def fee() -> uint256: view
    def coins(i: uint256) -> address: view

interface LT:
    def balanceOf(addr: address) -> uint256: view
    def withdraw(shares: uint256, min_assets: uint256, receiver: address) -> uint256: nonpayable
    def CRYPTOPOOL() -> CryptoPool: view
    def totalSupply() -> uint256: view

interface Erc20D:
    def decimals() -> uint8: view

interface NetPressureOracle:
    def net_pressure_and_tvl(lt: address, agg_price: uint256) -> PressureTvl: view


event Converted:
    lt: indexed(address)
    shares: uint256
    crvusd_out: uint256

event Returned:
    lt: indexed(address)
    assets: uint256

event SetSwapFeeMultiplier:
    swap_fee_multiplier: uint256


PRECISION: constant(uint256) = 10**18
FEE_DENOM: constant(uint256) = 10**10   # Curve pool fee() is scaled to 1e10

CRVUSD: public(immutable(IERC20))
NET_PRESSURE: public(immutable(NetPressureOracle))

swap_fee_multiplier: public(uint256)      # min = oracle * (1 - swap_fee_multiplier*pool_fee)

# cryptopool -> its asset already given an infinite approval (approve once, skip thereafter).
pool_approved: HashMap[CryptoPool, bool]


@deploy
def __init__(crvusd: IERC20, net_pressure: NetPressureOracle,
             swap_fee_multiplier: uint256, owner: address):
    """
    @param crvusd The crvUSD token (the conversion output).
    @param net_pressure The YBNetPressure oracle (the non-manipulable half-TVL for the withdraw floor).
    @param swap_fee_multiplier Slippage multiplier (1e18); min = oracle*(1 - multiplier*pool_fee).
    @param owner DAO address that may tune the slippage multiplier and sweep stuck tokens.
    """
    ownable.__init__()
    ownable._transfer_ownership(owner)
    CRVUSD = crvusd
    NET_PRESSURE = net_pressure
    self.swap_fee_multiplier = swap_fee_multiplier


@internal
def _ensure_pool_approval(pool: CryptoPool, asset: IERC20):
    if not self.pool_approved[pool]:
        assert extcall asset.approve(pool.address, max_value(uint256), default_return_value=True)
        self.pool_approved[pool] = True


@external
@nonreentrant
def convert(lt_addr: address) -> uint256:
    """
    @notice Pull the caller's shares of one LT (via transferFrom - approve this zap first),
            withdraw them to the pool asset and swap that to crvUSD (both legs oracle-bounded),
            sending the crvUSD to the caller. Permissionless. If the swap can't meet its min
            (e.g. a thin/off-peg pool) the swap error is swallowed and the withdrawn asset is
            handed to the caller instead. The zap pulls min(caller balance, caller allowance),
            so the approval caps the amount.
    @param lt_addr The LT token whose shares to pull from the caller and convert.
    @return crvUSD sent to the caller (0 if the swap was swallowed and the asset returned).
    """
    lt: LT = LT(lt_addr)
    amount: uint256 = min(staticcall lt.balanceOf(msg.sender),
                          staticcall IERC20(lt_addr).allowance(msg.sender, self))
    if amount == 0:
        return 0
    assert extcall IERC20(lt_addr).transferFrom(msg.sender, self, amount, default_return_value=True)

    pool: CryptoPool = staticcall lt.CRYPTOPOOL()
    asset: IERC20 = IERC20(staticcall pool.coins(1))
    p_o: uint256 = staticcall pool.price_oracle()
    discount: uint256 = min(self.swap_fee_multiplier * (staticcall pool.fee()) // FEE_DENOM, PRECISION)

    # 1) Withdraw, bounded by the price_oracle-fair value of the shares (half_tvl-based). A
    #    withdraw that can't meet its min reverts the whole call, so the caller keeps its shares.
    #    crvUSD ~ $1, so pass agg_price = 1.0 (the tiny aggregator deviation is well inside the
    #    slippage discount) and skip the Factory.agg() read.
    pt: PressureTvl = staticcall NET_PRESSURE.net_pressure_and_tvl(lt_addr, PRECISION)
    precision1: uint256 = 10 ** (18 - convert(staticcall Erc20D(asset.address).decimals(), uint256))
    fair_assets: uint256 = pt.half_tvl * amount // (staticcall lt.totalSupply()) * PRECISION // p_o // precision1
    min_assets: uint256 = fair_assets * (PRECISION - discount) // PRECISION
    asset_out: uint256 = extcall lt.withdraw(amount, min_assets, self)

    # 2) Swap asset -> crvUSD, bounded by the EMA price minus the same discount. Swallow a
    #    slippage revert (min_dy not met): hand the withdrawn asset to the caller and return 0.
    min_dy: uint256 = asset_out * p_o // PRECISION * (PRECISION - discount) // PRECISION
    self._ensure_pool_approval(pool, asset)
    success: bool = False
    response: Bytes[32] = b""
    success, response = raw_call(
        pool.address,
        abi_encode(convert(1, uint256), convert(0, uint256), asset_out, min_dy, self,
                   method_id=method_id("exchange(uint256,uint256,uint256,uint256,address)")),
        max_outsize=32, revert_on_failure=False)   # coin1 (asset) -> coin0 (crvUSD)
    if not success:
        assert extcall asset.transfer(msg.sender, asset_out, default_return_value=True)
        log Returned(lt=lt_addr, assets=asset_out)
        return 0

    crvusd_out: uint256 = abi_decode(response, uint256)
    assert extcall CRVUSD.transfer(msg.sender, crvusd_out, default_return_value=True)
    log Converted(lt=lt_addr, shares=amount, crvusd_out=crvusd_out)
    return crvusd_out


@external
def set_swap_fee_multiplier(swap_fee_multiplier: uint256):
    """
    @notice Set the slippage multiplier (1e18); a higher value loosens min_assets/min_dy so a
            thin/off-peg pool can still convert. DAO only.
    """
    ownable._check_owner()
    self.swap_fee_multiplier = swap_fee_multiplier
    log SetSwapFeeMultiplier(swap_fee_multiplier=swap_fee_multiplier)


@external
def recover(token: IERC20, to: address):
    """
    @notice Sweep any token held by this contract out to `to` (e.g. leftover shares/assets).
    @dev DAO only.
    """
    ownable._check_owner()
    assert extcall token.transfer(to, staticcall token.balanceOf(self), default_return_value=True)
