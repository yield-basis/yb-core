# @version 0.4.0

"""
LP oracle for cryptopools
"""

interface Cryptopool:
    def lp_price() -> uint256: view


POOL: public(immutable(Cryptopool))


@deploy
def __init__(pool: Cryptopool):
    POOL = pool


@external
@view
def price() -> uint256:
    return staticcall POOL.lp_price()


@external
def price_w() -> uint256:
    return staticcall POOL.lp_price()
