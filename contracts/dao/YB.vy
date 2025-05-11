# @version 0.4.1
"""
@title YBToken
@author Yield Basis
@license MIT
@notice YB Token
"""

from snekmate.auth import ownable
from snekmate.tokens import erc20
from snekmate.utils import math


initializes: ownable
initializes: erc20[ownable := ownable]


exports: (
    erc20.IERC20,
    erc20.IERC20Detailed,
    erc20.mint,
    ownable.renounce_ownership,
    erc20.set_minter
)


reserve: public(uint256)  # Reserve of tokens to be minted
last_minted: public(uint256)  # Last time minting has happened
max_mint_rate: public(immutable(uint256))  # Ceiling of the mint rate


@deploy
def __init__(reserve: uint256, max_rate: uint256):
    ownable.__init__()
    erc20.__init__("Yield Basis", "YB", 18, "Just say no", "to EIP712")
    # Ownership is now with msg.sender
    # The setup includes:
    # * Minting preallocations
    # * set_minter(GaugeController, True)
    # * set_minter(deployer, False)
    # * renounce_ownership(deployer)

    self.reserve = reserve
    max_mint_rate = max_rate


@internal
@view
def _emissions(t: uint256, rate_factor: uint256) -> uint256:
    assert rate_factor <= 10**18
    dt: int256 = convert(t - self.last_minted, int256)
    rate: int256 = convert(max_mint_rate * rate_factor // 10**18, int256)
    reserve: int256 = convert(self.reserve, int256)
    return convert(
        reserve - reserve * math._wad_exp(-dt * rate // 10**18) // 10**18,
        uint256)


@external
@view
def preview_emissions(t: uint256, rate_factor: uint256) -> uint256:
    return self._emissions(t, rate_factor)


@external
def emit(owner: address, rate_factor: uint256) -> uint256:
    """
    @dev Creates `amount` tokens and assigns them to `owner`.
    @notice Only authorised minters can access this function.
            Note that `owner` cannot be the zero address.
    @param owner The 20-byte owner address.
    @param rate_factor What percentage of inflation to mint (100% = 10**18)
    """
    assert erc20.is_minter[msg.sender], "erc20: access is denied"

    amount: uint256 = self._emissions(block.timestamp, rate_factor)
    self.reserve -= amount
    self.last_minted = block.timestamp

    erc20._mint(owner, amount)

    return amount
