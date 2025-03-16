# @version 0.4.1
"""
@title LT
@notice Factory for Yield Basis
@author Scientia Spectra AG
@license Copyright (c) 2025
"""
from ethereum.ercs import IERC20

# Creates the following:
# AMM
# LT
# Virtual pool for arbing easily
# Allocates stables

# Can deallocate stables as well
# fee receiver is in factory


interface LT:
    def set_amm(amm: address): nonpayable
    def set_rate(rate: uint256): nonpayable
    def allocate_stablecoins(allocator: address, limit: uint256): nonpayable

interface CurveCryptoPool:
    def coins(i: uint256) -> address: view


struct Market:
    collateral_token: IERC20
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

event SetAdmin:
    admin: address
    old_admin: address


MAX_MARKETS: public(constant(uint256)) = 50000

amm_impl: public(address)
lt_impl: public(LT)
virtual_pool_impl: public(address)
price_oracle_impl: public(address)
staker_impl: public(address)

STABLECOIN: public(immutable(IERC20))
fee_receiver: public(address)
admin: public(address)

markets: public(Market[MAX_MARKETS])
allocators: public(HashMap[address, uint256])
mint_factory: public(address)


@deploy
def __init__(
    stablecoin: IERC20,
    amm_impl: address,
    lt_impl: LT,
    virtual_pool_impl: address,
    price_oracle_impl: address,
    staker_impl: address,
    fee_receiver: address,
    admin: address
):
    STABLECOIN = stablecoin
    self.amm_impl = amm_impl
    self.lt_impl = lt_impl
    self.virtual_pool_impl = virtual_pool_impl
    self.price_oracle_impl = price_oracle_impl
    self.staker_impl = staker_impl
    self.fee_receiver = fee_receiver
    self.admin = admin

    log SetImplementations(amm=amm_impl, lt=lt_impl.address, virtual_pool=virtual_pool_impl, price_oracle=price_oracle_impl,
                           staker=staker_impl)


@external
def set_mint_factory(mint_factory: address):
    assert msg.sender == self.admin, "Access"
    assert self.mint_factory == empty(address), "Only set once"
    self.mint_factory = mint_factory
    # crvUSD factory can take back as much as it wants. Very important function - this is why it can be called only once
    extcall STABLECOIN.approve(mint_factory, max_value(uint256))

    log SetAllocator(allocator=mint_factory, amount=max_value(uint256))


@external
def set_allocator(allocator: address, amount: uint256):
    assert msg.sender == self.admin, "Access"
    assert allocator != self.mint_factory
    assert allocator != empty(address)

    old_allocation: uint256 = self.allocators[allocator]
    if amount > old_allocation:
        # Use transferFrom
        extcall STABLECOIN.transferFrom(allocator, self, amount - old_allocation)
    elif amount < old_allocation:
        # Allow to take back the allocation via transferFrom, but not more than the allocation reduction
        extcall STABLECOIN.approve(allocator, (staticcall STABLECOIN.allowance(self, allocator)) + old_allocation - amount)

    log SetAllocator(allocator=allocator, amount=amount)


@external
def set_admin(new_admin: address):
    assert msg.sender == self.admin, "Access"
    log SetAdmin(admin=new_admin, old_admin=self.admin)
    self.admin = new_admin


@external
def set_fee_receiver(new_fee_receiver: address):
    assert msg.sender == self.admin, "Access"
    self.fee_receiver = new_fee_receiver
    log SetFeeReceiver(fee_receiver=new_fee_receiver)
