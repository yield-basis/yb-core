# @version 0.4.3
"""
@title StakeZap
@author Yield Basis
@license MIT
"""
from ethereum.ercs import IERC20

interface LT:
    def deposit(assets: uint256, debt: uint256, min_shares: uint256) -> uint256: nonpayable
    def withdraw(shares: uint256, min_assets: uint256, receiver: address) -> uint256: nonpayable
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def ASSET_TOKEN() -> IERC20: view

interface LiquidityGauge:
    def deposit(assets: uint256, receiver: address) -> uint256: nonpayable
    def redeem(shares: uint256, receiver: address, owner: address) -> uint256: nonpayable
    def LP_TOKEN() -> LT: view
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable


approvals: HashMap[LiquidityGauge, bool]


@internal
def _approve_all(gauge: LiquidityGauge, lt: LT, asset: IERC20):
    if not self.approvals[gauge]:
        assert extcall asset.approve(lt.address, max_value(uint256), default_return_value=True)
        extcall lt.approve(gauge.address, max_value(uint256))
        self.approvals[gauge] = True


@external
def deposit_and_stake(gauge: LiquidityGauge, assets: uint256, debt: uint256, min_shares: uint256, receiver: address = msg.sender) -> uint256:
    lt: LT = staticcall gauge.LP_TOKEN()
    asset: IERC20 = staticcall lt.ASSET_TOKEN()
    self._approve_all(gauge, lt, asset)
    assert extcall asset.transferFrom(msg.sender, self, assets, default_return_value=True)
    lt_tokens: uint256 = extcall lt.deposit(assets, debt, min_shares)
    return extcall gauge.deposit(lt_tokens, receiver)


@external
def withdraw_and_unstake(gauge: LiquidityGauge, shares: uint256, min_assets: uint256, receiver: address = msg.sender) -> uint256:
    lt: LT = staticcall gauge.LP_TOKEN()
    asset: IERC20 = staticcall lt.ASSET_TOKEN()
    lt_tokens: uint256 = extcall gauge.redeem(shares, self, msg.sender)
    return extcall lt.withdraw(lt_tokens, min_assets, receiver)
