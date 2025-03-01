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
    def add_liquidity(amounts: uint256[2], min_mint_amount: uint256, receiver: address) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: uint256[2], receiver: address) -> uint256[2]: nonpayable

interface YbAMM:
    def coins(i: uint256) -> ERC20: view
    def exchange(i: uint256, j: uint256, in_amount: uint256, min_out: uint256, _for: address) -> uint256: nonpayable
    def STABLECOIN() -> ERC20: view
    def COLLATERAL() -> Pool: view


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
