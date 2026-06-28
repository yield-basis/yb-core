# @version 0.4.3
"""
@title MarketRateGetter
@notice Reports a "market rate" the PID controller compares its offered APR to.
        This implementation reads the Sky Savings Rate (sUSDS), which tracks a
        stable, low-risk savings yield that approximates the crvUSD risk premium.
@dev Swappable: the PID stores the getter address and the DAO can replace it with
     a different source. Only `rate()` is part of the interface other contracts use.
@license Copyright (c) 2025
"""


interface SUSDS:
    # Sky Savings Rate: per-second accumulation factor (1 + r) in RAY (1e27),
    # analogous to MakerDAO pot.dsr. Always >= RAY.
    def ssr() -> uint256: view


PRECISION: constant(uint256) = 10**18
RAY: constant(uint256) = 10**27
SECONDS_PER_YEAR: constant(uint256) = 365 * 86400

SUSDS_TOKEN: public(immutable(SUSDS))


@deploy
def __init__(susds: SUSDS):
    SUSDS_TOKEN = susds
    # Sanity-check the source on deploy (reverts if ssr() is missing/below RAY).
    assert staticcall susds.ssr() >= RAY, "Bad ssr"


@external
@view
def rate() -> uint256:
    """
    @notice Annualized (simple) market rate as a 1e18 fraction, e.g. 0.0354e18.
    @dev ssr is a per-second factor in RAY; per-second fraction is (ssr - RAY)/RAY,
         and the simple APR is that * SECONDS_PER_YEAR, rescaled RAY -> 1e18 (//1e9).
    """
    ssr: uint256 = staticcall SUSDS_TOKEN.ssr()
    return (ssr - RAY) * SECONDS_PER_YEAR // (RAY // PRECISION)
