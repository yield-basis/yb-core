# @version 0.4.3
"""
@title Factory
@notice Factory for Yield Basis
@author Scientia Spectra AG
@license Copyright (c) 2025
"""
from ethereum.ercs import IERC20
from ethereum.ercs import IERC20Detailed


interface LT:
    def set_amm(amm: address): nonpayable
    def set_rate(rate: uint256): nonpayable
    def set_staker(staker: address): nonpayable
    def allocate_stablecoins(limit: uint256): nonpayable

interface CurveCryptoPool:
    def coins(i: uint256) -> address: view
    def decimals() -> uint8: view

interface LPOracle:
    def AGG() -> address: view

interface Agg:
    def price() -> uint256: view

interface VirtualPool:
    def IMPL() -> address: view


struct Market:
    asset_token: IERC20
    cryptopool: CurveCryptoPool
    amm: address
    lt: address
    price_oracle: address
    virtual_pool: address
    staker: address

event SetImplementations:
    amm: address
    lt: address
    virtual_pool: address
    price_oracle: address
    staker: address

event SetAllocator:
    allocator: address
    amount: uint256

event SetFeeReceiver:
    fee_receiver: address

event SetAgg:
    agg: address

event SetFlash:
    flash: address

event SetAdmin:
    admin: address
    emergency_admin: address
    old_admin: address
    old_emergency_admin: address

event SetMinAdminFee:
    admin_fee: uint256

event SetGaugeController:
    gc: address

event MarketParameters:
    idx: indexed(uint256)
    asset_token: indexed(address)
    cryptopool: indexed(address)
    amm: address
    lt: address
    price_oracle: address
    virtual_pool: address
    staker: address
    agg: address


MAX_MARKETS: public(constant(uint256)) = 50000
LEVERAGE: public(constant(uint256)) = 2 * 10**18
gauge_controller: public(address)

amm_impl: public(address)
lt_impl: public(address)
virtual_pool_impl: public(address)
price_oracle_impl: public(address)
staker_impl: public(address)
agg: public(address)
flash: public(address)

STABLECOIN: public(immutable(IERC20))
fee_receiver: public(address)
admin: public(address)
emergency_admin: public(address)
min_admin_fee: public(uint256)

markets: public(Market[MAX_MARKETS])
market_count: public(uint256)
allocators: public(HashMap[address, uint256])
mint_factory: public(address)


@deploy
def __init__(
    stablecoin: IERC20,
    amm_impl: address,
    lt_impl: address,
    virtual_pool_impl: address,
    price_oracle_impl: address,
    staker_impl: address,
    agg: address,
    flash: address,
    fee_receiver: address,
    gauge_controller: address,
    admin: address,
    emergency_admin: address
):
    assert admin != empty(address)
    assert stablecoin.address != empty(address)
    assert agg != empty(address)
    assert price_oracle_impl != empty(address)
    assert amm_impl != empty(address)
    assert lt_impl != empty(address)

    assert staticcall IERC20Detailed(stablecoin.address).decimals() == 18

    STABLECOIN = stablecoin
    self.gauge_controller = gauge_controller
    self.amm_impl = amm_impl
    self.lt_impl = lt_impl
    self.virtual_pool_impl = virtual_pool_impl
    self.price_oracle_impl = price_oracle_impl
    self.staker_impl = staker_impl
    self.agg = agg
    self.flash = flash
    self.fee_receiver = fee_receiver
    self.admin = admin
    self.emergency_admin = emergency_admin
    self.min_admin_fee = 10**17

    self._validate_agg()

    log SetImplementations(amm=amm_impl, lt=lt_impl, virtual_pool=virtual_pool_impl, price_oracle=price_oracle_impl,
                           staker=staker_impl)


@internal
@view
def _validate_agg():
    p: uint256 = staticcall Agg(self.agg).price()
    assert p > 9 * 10**17 and p < 11 * 10**17, "Bad aggregator"


@external
@nonreentrant
def add_market(
    pool: CurveCryptoPool,
    fee: uint256,
    rate: uint256,
    debt_ceiling: uint256
) -> Market:
    assert msg.sender == self.admin, "Access"
    assert staticcall pool.coins(0) == STABLECOIN.address, "Wrong stablecoin"
    assert staticcall pool.decimals() == 18

    market: Market = empty(Market)
    agg: address = self.agg

    market.asset_token = IERC20(staticcall pool.coins(1))
    market.cryptopool = pool
    market.price_oracle = create_from_blueprint(self.price_oracle_impl, pool.address, agg)
    market.lt = create_from_blueprint(
        self.lt_impl,
        market.asset_token.address,
        STABLECOIN,
        pool.address,
        self
    )
    market.amm = create_from_blueprint(
        self.amm_impl,
        market.lt,
        STABLECOIN.address,
        pool.address,
        LEVERAGE,
        fee,
        market.price_oracle
    )
    extcall LT(market.lt).set_amm(market.amm)
    extcall LT(market.lt).set_rate(rate)
    assert extcall STABLECOIN.approve(market.lt, max_value(uint256), default_return_value=True)
    extcall LT(market.lt).allocate_stablecoins(debt_ceiling)

    if self.virtual_pool_impl != empty(address) and self.flash != empty(address):
        market.virtual_pool = create_from_blueprint(
            self.virtual_pool_impl,
            market.amm
        )
    if self.staker_impl != empty(address):
        market.staker = create_from_blueprint(
            self.staker_impl,
            market.lt)
        extcall LT(market.lt).set_staker(market.staker)

    i: uint256 = self.market_count
    if i < MAX_MARKETS:
        self.market_count = i + 1
    self.markets[i] = market

    log MarketParameters(
        idx=i,
        asset_token=market.asset_token.address,
        cryptopool=market.cryptopool.address,
        amm=market.amm,
        lt=market.lt,
        price_oracle=market.price_oracle,
        virtual_pool=market.virtual_pool,
        staker=market.staker,
        agg=agg
    )

    return market


@external
def fill_staker_vpool(i: uint256):
    assert msg.sender == self.admin, "Access"
    assert i < self.market_count, "Nonexistent market"

    market: Market = self.markets[i]
    assert market.lt != empty(address)
    assert market.amm != empty(address)

    new_virtual_pool: bool = False
    if market.virtual_pool == empty(address):
        new_virtual_pool = self.virtual_pool_impl != empty(address) and self.flash != empty(address)
    else:
        new_virtual_pool = (staticcall VirtualPool(market.virtual_pool).IMPL()) != self.virtual_pool_impl

    if new_virtual_pool:
        market.virtual_pool = create_from_blueprint(
            self.virtual_pool_impl,
            market.amm
        )

    if market.staker == empty(address) and self.staker_impl != empty(address):
        market.staker = create_from_blueprint(
            self.staker_impl,
            market.lt)
        extcall LT(market.lt).set_staker(market.staker)

    self.markets[i] = market

    log MarketParameters(
        idx=i,
        asset_token=market.asset_token.address,
        cryptopool=market.cryptopool.address,
        amm=market.amm,
        lt=market.lt,
        price_oracle=market.price_oracle,
        virtual_pool=market.virtual_pool,
        staker=market.staker,
        agg=(staticcall LPOracle(market.price_oracle).AGG())
    )


@external
@nonreentrant
def set_mint_factory(mint_factory: address):
    assert msg.sender == self.admin, "Access"
    assert self.mint_factory == empty(address), "Only set once"
    assert mint_factory != empty(address)
    self.mint_factory = mint_factory
    # crvUSD factory can take back as much as it wants. Very important function - this is why it can be called only once
    assert extcall STABLECOIN.approve(mint_factory, max_value(uint256), default_return_value=True)

    log SetAllocator(allocator=mint_factory, amount=max_value(uint256))


@external
@nonreentrant
def set_allocator(allocator: address, amount: uint256):
    assert msg.sender == self.admin, "Access"
    assert allocator != self.mint_factory, "Minter"
    assert allocator != empty(address)

    old_allocation: uint256 = self.allocators[allocator]
    if amount > old_allocation:
        # Use transferFrom
        assert extcall STABLECOIN.transferFrom(allocator, self, amount - old_allocation, default_return_value=True)
        self.allocators[allocator] = amount

    elif amount < old_allocation:
        # Allow to take back the allocation via transferFrom, but not more than the allocation reduction
        assert extcall STABLECOIN.approve(allocator, (staticcall STABLECOIN.allowance(self, allocator)) + old_allocation - amount, default_return_value=True)
        self.allocators[allocator] = amount

    log SetAllocator(allocator=allocator, amount=amount)


@external
def set_agg(agg: address):
    assert msg.sender == self.admin, "Access"
    assert agg != empty(address)
    self.agg = agg
    self._validate_agg()
    log SetAgg(agg=agg)


@external
def set_flash(flash: address):
    assert msg.sender == self.admin, "Access"
    self.flash = flash
    log SetFlash(flash=flash)


@external
def set_admin(new_admin: address, new_emergency_admin: address):
    assert msg.sender == self.admin, "Access"
    assert new_admin != empty(address)
    assert new_emergency_admin != empty(address)
    log SetAdmin(admin=new_admin, emergency_admin=new_emergency_admin, old_admin=self.admin, old_emergency_admin=self.emergency_admin)
    self.admin = new_admin
    self.emergency_admin = new_emergency_admin


@external
def set_fee_receiver(new_fee_receiver: address):
    assert msg.sender == self.admin, "Access"
    self.fee_receiver = new_fee_receiver
    log SetFeeReceiver(fee_receiver=new_fee_receiver)


@external
def set_gauge_controller(gc: address):
    assert msg.sender == self.admin, "Access"
    assert gc == empty(address), "Already set"
    self.gauge_controller = gc
    log SetGaugeController(gc=gc)


@external
def set_min_admin_fee(new_min_admin_fee: uint256):
    assert msg.sender == self.admin, "Access"
    assert new_min_admin_fee <= 10**18, "Admin fee too high"
    self.min_admin_fee = new_min_admin_fee
    log SetMinAdminFee(admin_fee=new_min_admin_fee)


@external
def set_implementations(amm: address, lt: address, virtual_pool: address, price_oracle: address, staker: address):
    assert msg.sender == self.admin, "Access"
    if amm != empty(address):
        self.amm_impl = amm
    if lt != empty(address):
        self.lt_impl = lt
    if virtual_pool != empty(address):
        self.virtual_pool_impl = virtual_pool
    if price_oracle != empty(address):
        self.price_oracle_impl = price_oracle
    if staker != empty(address):
        self.staker_impl = staker
    log SetImplementations(amm=amm, lt=lt, virtual_pool=virtual_pool, price_oracle=price_oracle, staker=staker)
