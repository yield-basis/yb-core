# @version 0.4.3
"""
@title YBPriceProxy
@notice Thin per-market price() forwarder. Each EIP-1167 clone binds one LT and one
        denomination (USD or asset) and re-exposes YBLendingOracle's view as price().
@dev Deployed once as the implementation; YBLendingOracle.create_oracles() clones it per
     market via create_minimal_proxy_to and calls initialize(). All math lives in the
     singleton oracle; a clone only holds (oracle, lt, in_usd) and staticcalls back.
@author Yield Basis
@license Copyright (c) 2025
"""

interface YBLendingOracle:
    def price_in_usd(lt: address, use_balances: bool) -> uint256: view
    def price_in_asset(lt: address, use_balances: bool) -> uint256: view


oracle: public(address)
lt: public(address)
in_usd: public(bool)


@external
def initialize(oracle: address, lt: address, in_usd: bool):
    """
    @notice One-time bind of the clone to its singleton oracle, LT and denomination.
    @param oracle The YBLendingOracle singleton this clone reads from
    @param lt The LT (market) this clone prices
    @param in_usd True to price in USD, False to price in the underlying asset
    """
    assert self.oracle == empty(address), "Initialized"
    assert oracle != empty(address) and lt != empty(address), "Zero"
    self.oracle = oracle
    self.lt = lt
    self.in_usd = in_usd


@external
@view
def price() -> uint256:
    """
    @notice Bound ybLT price - USD if in_usd else the underlying asset - scaled to 1e18.
    @dev Forwards to the singleton oracle's price_in_usd / price_in_asset for this LT.
    @return Price scaled to 1e18
    """
    if self.in_usd:
        return staticcall YBLendingOracle(self.oracle).price_in_usd(self.lt, False)
    return staticcall YBLendingOracle(self.oracle).price_in_asset(self.lt, False)
