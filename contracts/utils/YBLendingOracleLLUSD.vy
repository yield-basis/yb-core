# @version 0.4.3
"""
@title YBLendingOracleLLUSD
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice USD-denominated ybLT price oracle suitable for Curve LlamaLend
@dev Thin forwarder with no EMA state of its own: price() / price_w() map to the market's
     YBLendingOracleLL price_usd() / price_usd_w(). USD and asset consumers therefore
     checkpoint ONE shared fundamental EMA. Deployed per market by YBLendingOracleLLFactory
     via create_from_blueprint, with the oracle baked in as an immutable.
"""

interface LendingOracleLL:
    def price_usd() -> uint256: view
    def price_usd_w() -> uint256: nonpayable


ORACLE: public(immutable(LendingOracleLL))    # the market's YBLendingOracleLL (owns the EMA)


@deploy
def __init__(oracle: LendingOracleLL):
    """
    @param oracle The market's YBLendingOracleLL clone
    """
    assert oracle.address != empty(address), "Zero"
    ORACLE = oracle


@external
@view
def price() -> uint256:
    """
    @notice EMA-smoothed ybLT price in USD, scaled to 1e18.
    @return Smoothed price scaled to 1e18
    """
    return staticcall ORACLE.price_usd()


@external
def price_w() -> uint256:
    """
    @notice Checkpoint the shared EMA and return the smoothed ybLT price in USD.
    @return Smoothed price scaled to 1e18
    """
    return extcall ORACLE.price_usd_w()
