# @version 0.4.1

"""
LP oracle for cryptopools
"""

interface Cryptopool:
    def lp_price() -> uint256: view

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


@external
@view
def price() -> uint256:
    return staticcall POOL.lp_price() * staticcall AGG.price() // 10**18


@external
def price_w() -> uint256:
    return staticcall POOL.lp_price() * extcall AGG.price_w() // 10**18
