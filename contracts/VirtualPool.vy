# @version 0.4.3
"""
@title VirtualPool
@notice Virtual pool to swap LP in Yield Basis without touching the LP token
@author Scientia Spectra AG
@license Copyright (c) 2025
"""

from ethereum.ercs import IERC20 as ERC20

interface Flash:
    def flashLoan(receiver: address, token: address, amount: uint256, data: Bytes[10**5]) -> bool: nonpayable
    def supportedTokens(token: address) -> bool: view
    def maxFlashLoan(token: address) -> uint256: view

interface Factory:
    def flash() -> Flash: view
    def virtual_pool_impl() -> address: view

interface Pool:
    def approve(_spender: address, _value: uint256) -> bool: nonpayable
    def coins(i: uint256) -> ERC20: view
    def balances(i: uint256) -> uint256: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view
    def add_liquidity(amounts: uint256[2], min_mint_amount: uint256) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: uint256[2]) -> uint256[2]: nonpayable
    def totalSupply() -> uint256: view

interface YbAMM:
    def coins(i: uint256) -> ERC20: view
    def get_dy(i: uint256, j: uint256, in_amount: uint256) -> uint256: view
    def get_state() -> AMMState: view
    def fee() -> uint256: view
    def exchange(i: uint256, j: uint256, in_amount: uint256, min_out: uint256) -> uint256: nonpayable
    def STABLECOIN() -> ERC20: view
    def COLLATERAL() -> Pool: view


event TokenExchange:
    buyer: indexed(address)
    sold_id: uint256
    tokens_sold: uint256
    bought_id: uint256
    tokens_bought: uint256

struct AMMState:
    collateral: uint256
    debt: uint256
    x0: uint256


FACTORY: public(immutable(Factory))
AMM: public(immutable(YbAMM))
POOL: public(immutable(Pool))
ASSET_TOKEN: public(immutable(ERC20))
STABLECOIN: public(immutable(ERC20))
ROUNDING_DISCOUNT: public(constant(uint256)) = 10**18 // 10**8
IMPL: public(immutable(address))


@deploy
def __init__(amm: YbAMM):
    AMM = amm
    FACTORY = Factory(msg.sender)
    IMPL = staticcall FACTORY.virtual_pool_impl()
    POOL = staticcall amm.COLLATERAL()
    STABLECOIN = staticcall amm.STABLECOIN()
    assert staticcall POOL.coins(0) == STABLECOIN
    ASSET_TOKEN = staticcall POOL.coins(1)
    assert extcall STABLECOIN.approve(POOL.address, max_value(uint256), default_return_value=True)
    assert extcall ASSET_TOKEN.approve(POOL.address, max_value(uint256), default_return_value=True)
    assert extcall STABLECOIN.approve(AMM.address, max_value(uint256), default_return_value=True)
    assert extcall POOL.approve(AMM.address, max_value(uint256), default_return_value=True)


@external
@view
def coins(i: uint256) -> ERC20:
    return [STABLECOIN, ASSET_TOKEN][i]


@internal
@view
def _calculate(i: uint256, in_amount: uint256, only_flash: bool) -> (uint256, uint256):
    stables_in_pool: uint256 = staticcall POOL.balances(0)
    out_amount: uint256 = 0

    if i == 0:
        state: AMMState = staticcall AMM.get_state()
        pool_supply: uint256 = staticcall POOL.totalSupply()
        fee: uint256 = staticcall AMM.fee()
        r0fee: uint256 = stables_in_pool * (10**18 - fee) // pool_supply

        # Solving quadratic eqn instead of calling the AMM b/c we have a special case
        b: uint256 = state.x0 - state.debt + in_amount - r0fee * state.collateral // 10**18
        D: uint256 = b**2 + 4 * state.collateral * r0fee // 10**18 * in_amount
        flash_amount: uint256 = (isqrt(D) - b) // 2  # We received this withdrawing from the pool

        if not only_flash:
            crypto_in_pool: uint256 = staticcall POOL.balances(1)
            out_amount = flash_amount * crypto_in_pool // stables_in_pool
        # Withdrawal was ideally balanced
        return out_amount, flash_amount

    else:
        crypto_in_pool: uint256 = staticcall POOL.balances(1)
        flash_amount: uint256 = in_amount * stables_in_pool // crypto_in_pool
        if not only_flash:
            pool_supply: uint256 = staticcall POOL.totalSupply()
            lp_amount: uint256 = pool_supply * in_amount // crypto_in_pool
            out_amount = staticcall AMM.get_dy(1, 0, lp_amount) - flash_amount
        return out_amount, flash_amount


@external
@view
def get_dy(i: uint256, j: uint256, in_amount: uint256) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)
    _in_amount: uint256 = in_amount
    if i == 0:
        _in_amount = in_amount * (10**18 - ROUNDING_DISCOUNT) // 10**18
    return self._calculate(i, _in_amount, False)[0]


@external
def onFlashLoan(initiator: address, token: address, total_flash_amount: uint256, fee: uint256, data: Bytes[10**5]):
    assert initiator == self
    assert token == STABLECOIN.address
    assert msg.sender == (staticcall FACTORY.flash()).address, "Wrong caller"

    # executor
    i: uint256 = 0
    in_amount: uint256 = 0
    i, in_amount = abi_decode(data, (uint256, uint256))
    flash_amount: uint256 = self._calculate(i, in_amount, True)[1]
    in_coin: ERC20 = [STABLECOIN, ASSET_TOKEN][i]
    out_coin: ERC20 = [STABLECOIN, ASSET_TOKEN][1-i]
    repay_flash_amount: uint256 = total_flash_amount

    if i == 0:
        # stablecoin -> crypto exchange
        # 1. Take flash loan
        # 2. Use our stables + flash borrowed amount to swap to pool LP in AMM
        # 3. Withdraw symmetrically from pool LP
        # 4. Repay the flash loan
        # 5. Send the crypto
        lp_amount: uint256 = extcall AMM.exchange(0, 1, (in_amount * (10**18 - ROUNDING_DISCOUNT) // 10**18 + flash_amount), 0)
        extcall POOL.remove_liquidity(lp_amount, [0, 0])
        repay_flash_amount = staticcall STABLECOIN.balanceOf(self)

    else:
        # crypto -> stablecoin exchange
        # 1. Take flash loan
        # 2. Deposit taken stables + to Pool
        # 3. Swap LP of the pool to stables
        # 4. Repay flash loan
        # 5. Send the rest to the user
        lp_amount: uint256 = extcall POOL.add_liquidity([flash_amount, in_amount], 0)
        extcall AMM.exchange(1, 0, lp_amount, 0)

    assert extcall STABLECOIN.transfer(msg.sender, repay_flash_amount, default_return_value=True)

@external
@nonreentrant
def exchange(i: uint256, j: uint256, in_amount: uint256, min_out: uint256, _for: address = msg.sender) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)
    flash: Flash = staticcall FACTORY.flash()

    in_coin: ERC20 = [STABLECOIN, ASSET_TOKEN][i]
    out_coin: ERC20 = [STABLECOIN, ASSET_TOKEN][j]

    assert extcall in_coin.transferFrom(msg.sender, self, in_amount, default_return_value=True)

    data: Bytes[128] = empty(Bytes[128])
    data = abi_encode(i, in_amount)
    extcall flash.flashLoan(self, STABLECOIN.address, staticcall flash.maxFlashLoan(STABLECOIN.address), data)

    out_amount: uint256 = staticcall out_coin.balanceOf(self)
    assert out_amount >= min_out, "Slippage"
    assert extcall out_coin.transfer(_for, out_amount, default_return_value=True)

    log TokenExchange(buyer=_for, sold_id=i, tokens_sold=in_amount, bought_id=j, tokens_bought=out_amount)
    return out_amount


# XXX include methods to determine max_in / max_out
