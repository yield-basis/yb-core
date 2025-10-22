# @version 0.4.3


interface Factory:
    def set_admin(new_admin: address, new_emergency_admin: address): nonpayable
    def emergency_admin() -> address: view
    def add_market(pool: address, fee: uint256, rate: uint256, debt_ceiling: uint256): nonpayable
    def set_fee_receiver(new_fee_receiver: address): nonpayable
    def set_implementations(amm: address, lt: address, virtual_pool: address, price_oracle: address, staker: address): nonpayable
    def set_min_admin_fee(new_min_admin_fee: uint256): nonpayable

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view

interface AMM:
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def collateral_amount() -> uint256: view

interface LT:
    def set_rate(rate: uint256): nonpayable
    def set_amm_fee(fee: uint256): nonpayable
    def allocate_stablecoins(limit: uint256): nonpayable
    def amm() -> AMM: view
    def stablecoin_allocated() -> uint256: view
    def set_killed(is_killed: bool): nonpayable


ADMIN: public(immutable(address))
FACTORY: public(immutable(Factory))


@deploy
def __init__(admin: address, factory: Factory):
    ADMIN = admin
    FACTORY = factory


@external
def transfer_ownership_back():
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_admin(ADMIN, staticcall FACTORY.emergency_admin())


@external
def lt_set_rate(lt: LT, rate: uint256):
    assert msg.sender == ADMIN, "Access"
    extcall lt.set_rate(rate)


@external
def lt_set_amm_rate(lt: LT, fee: uint256):
    assert msg.sender == ADMIN, "Access"
    extcall lt.set_amm_fee(fee)


@external
def lt_allocate_stablecoins(lt: LT, limit: uint256 = max_value(uint256)):
    if limit != 0:
        assert msg.sender == ADMIN, "Access"
        extcall lt.allocate_stablecoins(limit)

    else:
        # Deallocate as much as available, and allow anyone to do it
        amm: AMM = staticcall lt.amm()
        lp_price: uint256 = extcall (staticcall amm.PRICE_ORACLE_CONTRACT()).price_w()
        available_limit: uint256 = lp_price * (staticcall amm.collateral_amount()) // 10**18
        allocated: uint256 = staticcall lt.stablecoin_allocated()
        assert available_limit < allocated, "Deflate"
        extcall lt.allocate_stablecoins(available_limit)


@external
def lt_set_killed(lt: LT, is_killed: bool):
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
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.add_market(pool, fee, rate, debt_ceiling)


@external
def set_fee_receiver(new_fee_receiver: address):
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_fee_receiver(new_fee_receiver)


@external
def set_implementations(amm: address, lt: address, virtual_pool: address, price_oracle: address, staker: address):
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_implementations(amm, lt, virtual_pool, price_oracle, staker)


@external
def set_min_admin_fee(new_min_admin_fee: uint256):
    assert msg.sender == ADMIN, "Access"
    extcall FACTORY.set_min_admin_fee(new_min_admin_fee)
