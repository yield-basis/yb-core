# @version 0.4.3

"""
@title CryptopoolLPOracle
@notice LP oracle for Curve cryptopools
@author Scientia Spectra AG
@license Copyright (c) 2025
"""

interface Cryptopool:
    def lp_price() -> uint256: view
    def virtual_price() -> uint256: view
    def price_scale() -> uint256: view

interface PriceOracle:
    def price() -> uint256: view
    def price_w() -> uint256: nonpayable


POOL: public(immutable(Cryptopool))
AGG: public(immutable(PriceOracle))


@deploy
def __init__(pool: Cryptopool, agg: PriceOracle):
    """
    @param pool Cryptopool crvUSD/crypto
    @param agg Price aggregator returning price of crvUSD in aggregated USD
    """
    POOL = pool
    AGG = agg


@internal
@view
def lp_price() -> uint256:
    virtual_price: uint256 = staticcall POOL.virtual_price()
    p_scale: uint256 = staticcall POOL.price_scale()
    return 2 * virtual_price * isqrt(p_scale * 10**18) // 10**18


@external
@view
def price() -> uint256:
    return self.lp_price() * staticcall AGG.price() // 10**18


@external
def price_w() -> uint256:
    return self.lp_price() * extcall AGG.price_w() // 10**18
