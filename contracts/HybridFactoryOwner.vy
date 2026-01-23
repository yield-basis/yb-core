# @version 0.4.3
"""
@title HybridFactoryOwner
@author Scientia Spectra AG
@license Copyright (c) 2025
@notice Admin proxy contract for managing HybridVaultFactory and its LT markets
"""
from ethereum.ercs import IERC20


interface Factory:
    def set_admin(new_admin: address, new_emergency_admin: address): nonpayable
    def emergency_admin() -> address: view
    def add_market(pool: address, fee: uint256, rate: uint256, debt_ceiling: uint256): nonpayable
    def set_fee_receiver(new_fee_receiver: address): nonpayable
    def set_implementations(amm: address, lt: address, virtual_pool: address, price_oracle: address, staker: address): nonpayable
    def set_min_admin_fee(new_min_admin_fee: uint256): nonpayable
    def STABLECOIN() -> IERC20: view

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view

interface AMM:
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def collateral_amount() -> uint256: view
    def value_oracle() -> OraclizedValue: view

interface Cryptopool:
    def price_scale() -> uint256: view

interface LT:
    def set_rate(rate: uint256): nonpayable
    def set_amm_fee(fee: uint256): nonpayable
    def allocate_stablecoins(limit: uint256): nonpayable
    def amm() -> AMM: view
    def stablecoin_allocated() -> uint256: view
    def set_killed(is_killed: bool): nonpayable
    def CRYPTOPOOL() -> Cryptopool: view
    def pricePerShare() -> uint256: view


struct OraclizedValue:
    p_o: uint256
    value: uint256


event SetLimitSetter:
    setter: address
    enabled: bool


ADMIN: public(immutable(address))
FACTORY: public(immutable(Factory))
STABLECOIN: public(immutable(IERC20))
disabled_lts: public(HashMap[LT, bool])
limit_setters: public(HashMap[address, bool])


@deploy
def __init__(admin: address, factory: Factory):
    """
    @notice Initialize the HybridFactoryOwner contract
    @param admin Address of the admin who can call privileged functions
    @param factory Address of the HybridVaultFactory to manage
    """
    ADMIN = admin
    FACTORY = factory
    STABLECOIN = staticcall factory.STABLECOIN()


@external
def transfer_ownership_back():
    """
    @notice Transfer factory ownership back to the admin
    @dev Only callable by admin. Restores admin control over the factory
    """
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_admin(ADMIN, staticcall FACTORY.emergency_admin())


@external
def lt_set_rate(lt: LT, rate: uint256):
    """
    @notice Set the interest rate for a specific LT market
    @param lt Address of the LT contract
    @param rate New interest rate to set
    """
    assert msg.sender == ADMIN, "Access"
    extcall lt.set_rate(rate)


@external
def lt_set_amm_fee(lt: LT, fee: uint256):
    """
    @notice Set the AMM fee for a specific LT market
    @param lt Address of the LT contract
    @param fee New AMM fee to set
    """
    assert msg.sender == ADMIN, "Access"
    extcall lt.set_amm_fee(fee)


@external
@view
def lt_needs_withdraw(lt: LT) -> uint256:
    """
    @notice Calculate the vault share amount that needs to be withdrawn to make liquidity matching safe crvUSD allocation limit
    @dev Returns the amount of LP tokens to withdraw so available limit matches allocated stablecoins
    @param lt Address of the LT contract
    @return Amount of vault shares to withdraw (0 if no withdrawal needed)
    """
    amm: AMM = staticcall lt.amm()
    lp_price: uint256 = staticcall (staticcall amm.PRICE_ORACLE_CONTRACT()).price()
    available_limit: uint256 = lp_price * (staticcall amm.collateral_amount()) // 10**18
    allocated: uint256 = staticcall lt.stablecoin_allocated()
    p_share: uint256 = (staticcall (staticcall lt.CRYPTOPOOL()).price_scale()) * (staticcall lt.pricePerShare()) // 10**18
    if available_limit >= allocated:
        return (available_limit - allocated + 1) * 10**18 // p_share
    else:
        return 0


@external
@view
def lt_in_factory(lt: address) -> bool:
    """
    @notice Check if an LT address is registered in the factory
    @dev Checks via stablecoin allowance from factory to LT
    @param lt Address to check
    @return True if the LT is in the factory
    """
    return (staticcall STABLECOIN.allowance(FACTORY.address, lt)) > 0


@external
def lt_allocate_stablecoins(lt: LT, limit: uint256 = max_value(uint256)):
    """
    @notice Allocate or deallocate stablecoins for an LT market
    @dev When limit > 0: Admin or limit setters can increase allocation.
         When limit = 0: Admin can disable LT; anyone can deallocate a disabled LT
         down to available reserves (must be >= 75% of oracle value)
    @param lt Address of the LT contract
    @param limit New stablecoin allocation limit (0 to deallocate, max_value for unlimited)
    """
    if limit != 0:
        assert msg.sender == ADMIN or self.limit_setters[msg.sender], "Access"
        self.disabled_lts[lt] = False
        extcall lt.allocate_stablecoins(limit)

    else:
        if msg.sender == ADMIN:
            self.disabled_lts[lt] = True

        else:
            assert self.disabled_lts[lt], "Not disabled"

            # Deallocate as much as available, and allow anyone to do it
            amm: AMM = staticcall lt.amm()
            lp_price: uint256 = extcall (staticcall amm.PRICE_ORACLE_CONTRACT()).price_w()
            available_limit: uint256 = lp_price * (staticcall amm.collateral_amount()) // 10**18
            allocated: uint256 = staticcall lt.stablecoin_allocated()
            safe_limit: uint256 = (staticcall amm.value_oracle()).value * 3 // 4
            if available_limit < allocated:  # Do not revert if we have less but do nothing
                assert available_limit >= safe_limit, "Not enough reserves"
                extcall lt.allocate_stablecoins(available_limit)


@external
def lt_set_killed(lt: LT, is_killed: bool):
    """
    @notice Enable or disable (kill) an LT market
    @param lt Address of the LT contract
    @param is_killed True to kill the market, False to re-enable
    """
    assert msg.sender == ADMIN, "Access"
    extcall lt.set_killed(is_killed)


## Factory methods

@external
def add_market(
    pool: address,
    fee: uint256,
    rate: uint256,
    debt_ceiling: uint256
):
    """
    @notice Add a new market to the factory
    @param pool Address of the crypto pool
    @param fee AMM fee for the market
    @param rate Interest rate for borrowing
    @param debt_ceiling Maximum debt ceiling for the market
    """
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.add_market(pool, fee, rate, debt_ceiling)


@external
def set_fee_receiver(new_fee_receiver: address):
    """
    @notice Set the fee receiver address in the factory
    @param new_fee_receiver Address that will receive collected fees
    """
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_fee_receiver(new_fee_receiver)


@external
def set_implementations(amm: address, lt: address, virtual_pool: address, price_oracle: address, staker: address):
    """
    @notice Set implementation addresses for factory deployments
    @param amm AMM implementation address
    @param lt LT (vault) implementation address
    @param virtual_pool Virtual pool implementation address
    @param price_oracle Price oracle implementation address
    @param staker Staker implementation address
    """
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_implementations(amm, lt, virtual_pool, price_oracle, staker)


@external
def set_min_admin_fee(new_min_admin_fee: uint256):
    """
    @notice Set the minimum admin fee in the factory
    @param new_min_admin_fee New minimum admin fee value
    """
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_min_admin_fee(new_min_admin_fee)


@external
def set_limit_setter(setter: address, enabled: bool):
    """
    @notice Grant or revoke limit setter privileges for an address
    @dev Limit setters can call lt_allocate_stablecoins with non-zero limits
    @param setter Address to grant/revoke privileges
    @param enabled True to grant, False to revoke
    """
    assert msg.sender == ADMIN, "Access"
    self.limit_setters[setter] = enabled
    log SetLimitSetter(setter=setter, enabled=enabled)
