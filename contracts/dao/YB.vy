# @version 0.4.3
"""
@title YBToken
@author Yield Basis
@license GNU Affero General Public License v3.0
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
    erc20.is_minter,
    erc20.set_minter,
    erc20.transfer_ownership,
    ownable.owner
)


reserve: public(uint256)  # Reserve of tokens to be minted
last_minted: public(uint256)  # Last time minting has happened
max_mint_rate: public(immutable(uint256))  # Ceiling of the mint rate


@deploy
def __init__(reserve: uint256, max_rate: uint256):
    """
    @param reserve Amount of YB tokens to emit over infinite time
    @param max_rate Maximum emission rate in tokens per second (reached when rate_factor=1.0)
    """
    ownable.__init__()
    erc20.__init__("Yield Basis", "YB", 18, "Just say no", "to EIP712")
    # Ownership is now with msg.sender
    # The setup includes:
    # * Minting preallocations
    # * set_minter(GaugeController, True)
    # * renounce_ownership(deployer) - will also unset the minter

    self.reserve = reserve
    max_mint_rate = max_rate * 10**18 // reserve


@internal
@view
def _emissions(t: uint256, rate_factor: uint256) -> uint256:
    assert rate_factor <= 10**18
    last_minted: uint256 = self.last_minted
    if last_minted == 0 or t <= last_minted:
        return 0
    else:
        dt: int256 = convert(t - last_minted, int256)
        rate_36: int256 = convert(max_mint_rate * rate_factor, int256)
        reserve: int256 = convert(self.reserve, int256)
        return convert(
            reserve * (10**18 - math._wad_exp(-dt * rate_36 // 10**18)) // 10**18,
            uint256)


@external
@view
def preview_emissions(t: uint256, rate_factor: uint256) -> uint256:
    """
    @notice Calculate the amount of emissions to be released by the time t
    @param t Time for which the emissions should be calculated
    @param rate_factor Average rate factor from 0.0 (0) to 1.0 (1e18)
    """
    return self._emissions(t, rate_factor)


@external
def start_emissions():
    """
    @notice Start token emissions. Ownership must be renounced no earlier than emissions are started
    """
    ownable._check_owner()
    if self.last_minted == 0:
        self.last_minted = block.timestamp


@external
def renounce_ownership():
    """
    @notice Method for deployer to renounce ownership of the token. After this only GaugeController can mint
    """
    ownable._check_owner()
    # Force-strt emissions when renouncing ownership
    if self.last_minted == 0:
        self.last_minted = block.timestamp
    erc20.is_minter[msg.sender] = False
    log erc20.RoleMinterChanged(minter=msg.sender, status=False)
    ownable._transfer_ownership(empty(address))


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

    amount: uint256 = 0

    if self.last_minted > 0:
        amount = self._emissions(block.timestamp, rate_factor)
        self.reserve -= amount
        self.last_minted = block.timestamp
        erc20._mint(owner, amount)

    return amount
