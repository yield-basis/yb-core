# @version 0.4.3
"""
@title YBGauge
@author Yield Basis
@license MIT
@notice Implementation contract inspired by Curve Finance gauges, but without boosts
"""
from ethereum.ercs import IERC20
from snekmate.extensions import erc4626


initializes: erc4626

exports: (
    erc4626.IERC20,
    erc4626.IERC4626,
    erc4626.decimals,
)


interface IERC20Slice:
    def symbol() -> String[30]: view
    def name() -> String[57]: view


get_adjustment: public(uint256)


@deploy
@payable
def __init__(lt: IERC20):
    erc4626.__init__("", "", lt, 0, "burn baby", "burn gas")


@external
@view
def symbol() -> String[32]:
    return concat('g-', staticcall IERC20Slice(erc4626.asset).symbol())


@external
@view
def name() -> String[64]:
    return concat('Gauge: ', staticcall IERC20Slice(erc4626.asset).name())


@external
def set_adjustment(adjustment: uint256):
    self.get_adjustment = adjustment
