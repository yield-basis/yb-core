# @version 0.4.1
"""
@title Virtual pool to swap LP in Yield Basis without touching the LP token
"""

from ethereum.ercs import IERC20 as ERC20

interface Flash:
    def flashLoan(receiver: address, token: address, amount: uint256, data: Bytes[10**5]) -> bool: view
    def supportedTokens(token: address) -> bool: view

interface Pool:
    def coins(i: uint256) -> ERC20: view
    def balances(i: uint256) -> uint256: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view
    def add_liquidity(amounts: uint256[2], min_mint_amount: uint256, receiver: address) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: uint256[2], receiver: address) -> uint256[2]: nonpayable
    def totalSupply() -> uint256: view

interface YbAMM:
    def coins(i: uint256) -> ERC20: view
    def get_dy(i: uint256, j: uint256, in_amount: uint256) -> uint256: view
    def get_state() -> AMMState: view
    def fee() -> uint256: view
    def exchange(i: uint256, j: uint256, in_amount: uint256, min_out: uint256, _for: address) -> uint256: nonpayable
    def STABLECOIN() -> ERC20: view
    def COLLATERAL() -> Pool: view


struct AMMState:
    collateral: uint256
    debt: uint256
    x0: uint256


FLASH: public(immutable(Flash))
AMM: public(immutable(YbAMM))
POOL: public(immutable(Pool))
CRYPTO: public(immutable(ERC20))
STABLECOIN: public(immutable(ERC20))


@deploy
def __init__(amm: YbAMM, flash: Flash):
    AMM = amm
    FLASH = flash
    POOL = staticcall amm.COLLATERAL()
    STABLECOIN = staticcall amm.STABLECOIN()
    assert staticcall POOL.coins(0) == STABLECOIN
    CRYPTO = staticcall POOL.coins(1)


@external
@view
def coins(i: uint256) -> ERC20:
    return [STABLECOIN, CRYPTO][i]


@external
@view
def get_dy(i: uint256, j: uint256, in_amount: uint256) -> uint256:
    assert (i == 0 and j == 1) or (i == 1 and j == 0)
    if i == 0 and j == 1:
        state: AMMState = staticcall AMM.get_state()
        fee: uint256 = staticcall AMM.fee()
        r0fee: uint256 = staticcall POOL.balances(0) * (10**18 - fee) // staticcall POOL.totalSupply()

        # Solving quadratic eqn
        b: uint256 = state.x0 - state.debt + in_amount - r0fee * state.collateral // 10**18
        D: uint256 = b**2 + 4 * state.collateral * r0fee // 10**18 * in_amount

        return (isqrt(D) - b) // 2

    elif i == 1 and j == 0:
        return 0

    else:
        raise "i!=j"
