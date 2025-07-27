# pragma version ~=0.4.3
# pragma nonreentrancy off
"""
@title ERC4626
@author Yield Basis
@license GNU Affero General Public License v3.0 only
@notice Library implementing ERC4626 standard derived from snekmate implenmentation.
        Unlike snekmate, however, it does not use offset and instead has a limit on minimal deposit allowed
"""


# @dev We import and implement the `IERC20` interface,
# which is a built-in interface of the Vyper compiler.
from ethereum.ercs import IERC20
implements: IERC20


# @dev We import and implement the `IERC20Detailed` interface,
# which is a built-in interface of the Vyper compiler.
from ethereum.ercs import IERC20Detailed
implements: IERC20Detailed


# @dev We import and implement the `IERC20Permit`
# interface, which is written using standard Vyper
# syntax.
from snekmate.tokens.interfaces import IERC20Permit
implements: IERC20Permit


# @dev We import and implement the `IERC4626` interface,
# which is a built-in interface of the Vyper compiler.
from ethereum.ercs import IERC4626
implements: IERC4626


# @dev We import and implement the `IERC5267` interface,
# which is written using standard Vyper syntax.
from snekmate.utils.interfaces import IERC5267
implements: IERC5267


# @dev We import the `math` module.
# @notice Please note that the `math` module is stateless
# and therefore does not require the `uses` keyword for usage.
from snekmate.utils import math


# @dev We import and initialise the `ownable` module.
# @notice The `ownable` module is merely used to initialise
# the `erc20` module, but none of the associated functions
# are exported.
from snekmate.auth import ownable
initializes: ownable


# @dev We import and initialise the `erc20` module.
from snekmate.tokens import erc20
initializes: erc20[ownable := ownable]


# @dev We export (i.e. the runtime bytecode exposes these
# functions externally, allowing them to be called using
# the ABI encoding specification) the required `external`
# functions from the `erc20` module to implement a compliant
# ERC-20 + EIP-2612 token.
# @notice Please note that you must always also export (if
# required by the contract logic) `public` declared `constant`,
# `immutable`, and state variables, for which Vyper automatically
# generates an `external` getter function for the variable.
exports: (
    erc20.totalSupply,
    erc20.balanceOf,
    erc20.transfer,
    erc20.transferFrom,
    erc20.approve,
    erc20.allowance,
    erc20.name,
    erc20.symbol,
    erc20.decimals,
    erc20.permit,
    erc20.nonces,
    erc20.DOMAIN_SEPARATOR,
    erc20.eip712Domain,
)


# @dev Returns the address of the underlying token
# used for the vault for accounting, depositing,
# and withdrawing. To preserve consistency with the
# ERC-4626 interface, we use lower case letters for
# the `immutable` variable `asset`.
# @notice Vyper returns the `address` type for interface
# types by default. Furthermore, if you declare a
# variable as `public`, Vyper automatically generates
# an `external` getter function for the variable.
asset: public(immutable(address))


# @dev Stores the ERC-20 interface object of the underlying
# token used for the vault for accounting, depositing,
# and withdrawing.
_ASSET: immutable(IERC20)


# @ dev Minimal amount of shares allowed to exist (unless it is zero).
# The objective is to prevent inflation attacks on integrations

MIN_SHARES: public(immutable(uint256))



@deploy
@payable
def __init__(
    name_: String[25],
    symbol_: String[5],
    asset_: IERC20,
    min_shares_decimals_: uint8,
    name_eip712_: String[50],
    version_eip712_: String[20],
):
    """
    @dev To omit the opcodes for checking the `msg.value`
         in the creation-time EVM bytecode, the constructor
         is declared as `payable`.
    @param name_ The maximum 25-character user-readable
           string name of the token.
    @param symbol_ The maximum 5-character user-readable
           string symbol of the token.
    @param asset_ The ERC-20 compatible (i.e. ERC-777 is also viable)
           underlying asset contract.
    @param min_shares_decimals_ 1-byte vaulue to determine MIN_SHARES = 10 ** min_shares_decimals_
    @param name_eip712_ The maximum 50-character user-readable
           string name of the signing domain, i.e. the name
           of the dApp or protocol.
    @param version_eip712_ The maximum 20-character current
           main version of the signing domain. Signatures
           from different versions are not compatible.
    """
    _ASSET = asset_
    asset = _ASSET.address

    success: bool = empty(bool)
    decoded_decimals: uint8 = empty(uint8)
    # Attempt to fetch the underlying's decimals. A return
    # value of `False` indicates that the attempt failed in
    # some way.
    success, decoded_decimals = self._try_get_underlying_decimals(asset_)
    decoded_decimals = decoded_decimals if success else 18

    MIN_SHARES = 10 ** convert(min_shares_decimals_, uint256)

    # Please note that the `ownable` module is merely used to
    # initialise the `erc20` module, but none of the associated
    # functions are exported.
    ownable.__init__()
    # The following line uses intentionally checked arithmetic
    # to prevent a theoretically possible overflow.
    erc20.__init__(name_, symbol_, decoded_decimals, name_eip712_, version_eip712_)


@external
@view
def totalAssets() -> uint256:
    """
    @dev Returns the total amount of the underlying asset
         that is managed by the vault.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#totalassets.
    @return uint256 The 32-byte total managed assets.
    """
    return self._total_assets()


@external
@view
def convertToShares(assets: uint256) -> uint256:
    """
    @dev Returns the amount of shares that the vault would
         exchange for the amount of assets provided, in an
         ideal scenario where all the conditions are met.
    @notice Note that the conversion must round down to `0`.
            For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#converttoshares.
    @param assets The 32-byte assets amount.
    @return uint256 The converted 32-byte shares amount.
    """
    return self._convert_to_shares(assets, False)


@external
@view
def convertToAssets(shares: uint256) -> uint256:
    """
    @dev Returns the amount of assets that the vault would
         exchange for the amount of shares provided, in an
         ideal scenario where all the conditions are met.
    @notice Note that the conversion must round down to `0`.
            For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#converttoassets.
    @param shares The 32-byte shares amount.
    @return uint256 The converted 32-byte assets amount.
    """
    return self._convert_to_assets(shares, False)


@external
@view
def maxDeposit(receiver: address) -> uint256:
    """
    @dev Returns the maximum amount of the underlying asset
         that can be deposited into the vault for the `receiver`,
         through a `deposit` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxdeposit.
    @param receiver The 20-byte receiver address.
    @return uint256 The 32-byte maximum deposit amount.
    """
    return self._max_deposit(receiver)


@external
@view
def previewDeposit(assets: uint256) -> uint256:
    """
    @dev Allows an on-chain or off-chain user to simulate the
         effects of their deposit at the current block, given
         current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewdeposit.
    @param assets The 32-byte assets amount.
    @return uint256 The simulated 32-byte returning shares amount.
    """
    return self._preview_deposit(assets)


@external
def deposit(assets: uint256, receiver: address) -> uint256:
    """
    @dev Mints `shares` vault shares to `receiver` by depositing
         exactly `assets` of underlying tokens.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#deposit.
    @param assets The 32-byte assets amount.
    @param receiver The 20-byte receiver address.
    @return uint256 The 32-byte shares amount to be created.
    """
    assert assets <= self._max_deposit(receiver), "erc4626: deposit more than maximum"
    shares: uint256 = self._preview_deposit(assets)
    self._deposit(msg.sender, receiver, assets, shares)
    self._check_min_shares()
    return shares


@external
@view
def maxMint(receiver: address) -> uint256:
    """
    @dev Returns the maximum amount of shares that can be minted
         from the vault for the `receiver`, through a `mint` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxmint.
    @param receiver The 20-byte receiver address.
    @return uint256 The 32-byte maximum mint amount.
    """
    return self._max_mint(receiver)


@external
@view
def previewMint(shares: uint256) -> uint256:
    """
    @dev Allows an on-chain or off-chain user to simulate the
         effects of their `mint` at the current block, given
         current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewmint.
    @param shares The 32-byte shares amount.
    @return uint256 The simulated 32-byte required assets amount.
    """
    return self._preview_mint(shares)


@external
def mint(shares: uint256, receiver: address) -> uint256:
    """
    @dev Mints exactly `shares` vault shares to `receiver` by
         depositing `assets` of underlying tokens.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#mint.
    @param shares The 32-byte shares amount to be created.
    @param receiver The 20-byte receiver address.
    @return uint256 The deposited 32-byte assets amount.
    """
    assert shares <= self._max_mint(receiver), "erc4626: mint more than maximum"
    assets: uint256 = self._preview_mint(shares)
    self._deposit(msg.sender, receiver, assets, shares)
    self._check_min_shares()
    return assets


@external
@view
def maxWithdraw(owner: address) -> uint256:
    """
    @dev Returns the maximum amount of the underlying asset that
         can be withdrawn from the owner balance in the vault,
         through a `withdraw` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxwithdraw.
    @param owner The 20-byte owner address.
    @return uint256 The 32-byte maximum withdraw amount.
    """
    return self._max_withdraw(owner)


@external
@view
def previewWithdraw(assets: uint256) -> uint256:
    """
    @dev Allows an on-chain or off-chain user to simulate the
         effects of their withdrawal at the current block, given
         current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewwithdraw.
    @param assets The 32-byte assets amount.
    @return uint256 The simulated 32-byte burned shares amount.
    """
    return self._preview_withdraw(assets)


@external
def withdraw(assets: uint256, receiver: address, owner: address) -> uint256:
    """
    @dev Burns `shares` from `owner` and sends exactly `assets` of
         underlying tokens to `receiver`.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#withdraw.
    @param assets The 32-byte assets amount to be withdrawn.
    @param receiver The 20-byte receiver address.
    @param owner The 20-byte owner address.
    @return uint256 The burned 32-byte shares amount.
    """
    assert assets <= self._max_withdraw(owner), "erc4626: withdraw more than maximum"
    shares: uint256 = self._preview_withdraw(assets)
    self._withdraw(msg.sender, receiver, owner, assets, shares)
    self._check_min_shares()
    return shares


@external
@view
def maxRedeem(owner: address) -> uint256:
    """
    @dev Maximum amount of vault shares that can be redeemed from
         the `owner` balance in the vault, through a `redeem` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxredeem.
    @param owner The 20-byte owner address.
    @return uint256 The 32-byte maximum redeemable shares amount.
    """
    return self._max_redeem(owner)


@external
@view
def previewRedeem(shares: uint256) -> uint256:
    """
    @dev Allows an on-chain or off-chain user to simulate the effects
         of their redeemption at the current block, given current
         on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewredeem.
    @param shares The 32-byte shares amount to be redeemed.
    @return uint256 The simulated 32-byte returning assets amount.
    """
    return self._preview_redeem(shares)


@external
def redeem(shares: uint256, receiver: address, owner: address) -> uint256:
    """
    @dev Burns exactly `shares` from `owner` and sends `assets` of
         underlying tokens to `receiver`.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#redeem.
    @param shares The 32-byte redeemed shares amount.
    @param receiver The 20-byte receiver address.
    @param owner The 20-byte owner address.
    @return uint256 The returned 32-byte assets amount.
    """
    assert shares <= self._max_redeem(owner), "erc4626: redeem more than maximum"
    assets: uint256 = self._preview_redeem(shares)
    self._withdraw(msg.sender, receiver, owner, assets, shares)
    self._check_min_shares()
    return assets


@internal
@view
def _try_get_underlying_decimals(underlying: IERC20) -> (bool, uint8):
    """
    @dev Attempts to fetch the underlying's decimals. A return
         value of `False` indicates that the attempt failed in
         some way.
    @param underlying The ERC-20 compatible (i.e. ERC-777 is also viable)
           underlying asset contract.
    @return bool The verification whether the call succeeded or
            failed.
    @return uint8 The fetched underlying's decimals.
    """
    success: bool = empty(bool)
    return_data: Bytes[32] = b""
    # The following low-level call does not revert, but instead
    # returns `False` if the callable contract does not implement
    # the `decimals` function. Since we perform a length check of
    # 32 bytes for the return data in the return expression at the
    # end, we also return `False` for EOA wallets instead of reverting
    # (remember that the EVM always considers a call to an EOA as
    # successful with return data `0x`). Furthermore, it is important
    # to note that an external call via `raw_call` does not perform an
    # external code size check on the target address.
    success, return_data = raw_call(
        underlying.address, method_id("decimals()"), max_outsize=32, is_static_call=True, revert_on_failure=False
    )
    if success and len(return_data) == 32 and convert(return_data, uint256) <= convert(max_value(uint8), uint256):
        return (True, convert(return_data, uint8))
    return (False, empty(uint8))


@internal
def _check_min_shares():
    supply: uint256 = erc20.totalSupply
    assert supply >= MIN_SHARES or supply == 0, "erc4626: leave MIN_SHARES"


@internal
@view
def _total_assets() -> uint256:
    """
    @dev An `internal` helper function that returns the total amount
         of the underlying asset that is managed by the vault.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#totalassets.
    @return uint256 The 32-byte total managed assets.
    """
    return staticcall _ASSET.balanceOf(self)


@internal
@view
def _convert_to_shares(assets: uint256, roundup: bool) -> uint256:
    """
    @dev An `internal` conversion function (from assets to shares)
         with support for rounding direction.
    @param assets The 32-byte assets amount.
    @param roundup The Boolean variable that specifies whether
           to round up or not. The default `False` is round down.
    @return uint256 The converted 32-byte shares amount.
    """
    supply: uint256 = erc20.totalSupply
    if supply == 0:
        return assets + self._total_assets()
    else:
        return math._mul_div(
            assets, supply + 1, self._total_assets() + 1, roundup
        )


@internal
@view
def _convert_to_assets(shares: uint256, roundup: bool) -> uint256:
    """
    @dev An `internal` conversion function (from shares to assets)
         with support for rounding direction.
    @param shares The 32-byte shares amount.
    @param roundup The Boolean variable that specifies whether
           to round up or not. The default `False` is round down.
    @return uint256 The converted 32-byte assets amount.
    """
    supply: uint256 = erc20.totalSupply
    if supply == 0:
        return shares - self._total_assets()
    else:
        return math._mul_div(
            shares, self._total_assets() + 1, supply + 1, roundup
        )


@internal
@pure
def _max_deposit(receiver: address) -> uint256:
    """
    @dev An `internal` helper function that returns the maximum
         amount of the underlying asset that can be deposited into
         the vault for the `receiver`, through a `deposit` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxdeposit.
    @param receiver The 20-byte receiver address.
    @return uint256 The 32-byte maximum deposit amount.
    """
    return max_value(uint256)


@internal
@view
def _preview_deposit(assets: uint256) -> uint256:
    """
    @dev An `internal` helper function that allows an on-chain or
         off-chain user to simulate the effects of their deposit at
         the current block, given current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewdeposit.
    @param assets The 32-byte assets amount.
    @return uint256 The simulated 32-byte returning shares amount.
    """
    return self._convert_to_shares(assets, False)


@internal
@pure
def _max_mint(receiver: address) -> uint256:
    """
    @dev An `internal` helper function that returns the maximum
         amount of shares that can be minted from the vault for
         the `receiver`, through a `mint` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxmint.
    @param receiver The 20-byte receiver address.
    @return uint256 The 32-byte maximum mint amount.
    """
    return max_value(uint256)


@internal
@view
def _preview_mint(shares: uint256) -> uint256:
    """
    @dev An `internal` helper function that allows an on-chain or
         off-chain user to simulate the effects of their `mint` at
         the current block, given current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewmint.
    @param shares The 32-byte shares amount.
    @return uint256 The simulated 32-byte required assets amount.
    """
    return self._convert_to_assets(shares, True)


@internal
@view
def _max_withdraw(owner: address) -> uint256:
    """
    @dev An `internal` helper function that returns the maximum
         amount of the underlying asset that can be withdrawn from
         the owner balance in the vault, through a `withdraw` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxwithdraw.
    @param owner The 20-byte owner address.
    @return uint256 The 32-byte maximum withdraw amount.
    """
    return self._convert_to_assets(erc20.balanceOf[owner], False)


@internal
@view
def _preview_withdraw(assets: uint256) -> uint256:
    """
    @dev An `internal` helper function that allows an on-chain or
         off-chain user to simulate the effects of their withdrawal
         at the current block, given current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewwithdraw.
    @param assets The 32-byte assets amount.
    @return uint256 The simulated 32-byte burned shares amount.
    """
    return self._convert_to_shares(assets, True)


@internal
@view
def _max_redeem(owner: address) -> uint256:
    """
    @dev An `internal` helper function that returns the maximum
         amount of vault shares that can be redeemed from the `owner`
         balance in the vault, through a `redeem` call.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#maxredeem.
    @param owner The 20-byte owner address.
    @return uint256 The 32-byte maximum redeemable shares amount.
    """
    return erc20.balanceOf[owner]


@internal
@view
def _preview_redeem(shares: uint256) -> uint256:
    """
    @dev An `internal` helper function that allows an on-chain or
         off-chain user to simulate the effects of their redeemption
         at the current block, given current on-chain conditions.
    @notice For the to be fulfilled conditions, please refer to:
            https://eips.ethereum.org/EIPS/eip-4626#previewredeem.
    @param shares The 32-byte shares amount to be redeemed.
    @return uint256 The simulated 32-byte returning assets amount.
    """
    return self._convert_to_assets(shares, False)


@internal
def _deposit(sender: address, receiver: address, assets: uint256, shares: uint256):
    """
    @dev An `internal` function handling the `deposit` and `mint`
         common workflow.
    @param sender The 20-byte sender address.
    @param receiver The 20-byte receiver address.
    @param assets The 32-byte assets amount.
    @param shares The 32-byte shares amount.
    """
    # If `asset` is an ERC-777, `transferFrom` can trigger a reentrancy
    # before the transfer happens through the `tokensToSend` hook. On the
    # other hand, the `tokenReceived` hook, that is triggered after the
    # transfer, calls the vault which is assumed not to be malicious.
    # Thus, we need to do the transfer before we mint so that any reentrancy
    # would happen before the assets are transferred and before the shares
    # are minted, which is a valid state.

    # To deal with (potentially) non-compliant ERC-20 tokens that do have
    # no return value, we use the kwarg `default_return_value` for external
    # calls. This function was introduced in Vyper version `0.3.4`. For more
    # details see:
    # - https://github.com/vyperlang/vyper/pull/2839,
    # - https://github.com/vyperlang/vyper/issues/2812,
    # - https://medium.com/coinmonks/missing-return-value-bug-at-least-130-tokens-affected-d67bf08521ca.

    # It is important to note that an external call via interface casting
    # always performs an external code size check on the target address unless
    # you add the kwarg `skip_contract_check=True`. If the check fails (i.e.
    # the target address is an EOA), the call reverts.
    assert extcall _ASSET.transferFrom(
        sender, self, assets, default_return_value=True
    ), "erc4626: transferFrom operation did not succeed"
    erc20._mint(receiver, shares)
    log IERC4626.Deposit(sender=sender, owner=receiver, assets=assets, shares=shares)


@internal
def _withdraw(sender: address, receiver: address, owner: address, assets: uint256, shares: uint256):
    """
    @dev An `internal` function handling the `withdraw` and `redeem`
         common workflow.
    @param sender The 20-byte sender address.
    @param receiver The 20-byte receiver address.
    @param owner The 20-byte owner address.
    @param assets The 32-byte assets amount.
    @param shares The 32-byte shares amount.
    """
    if sender != owner:
        erc20._spend_allowance(owner, sender, shares)

    # If `asset` is an ERC-777, `transfer` can trigger a reentrancy
    # after the transfer happens through the `tokensReceived` hook.
    # On the other hand, the `tokensToSend` hook, that is triggered
    # before the transfer, calls the vault which is assumed not to
    # be malicious. Thus, we need to do the transfer after the burn
    # so that any reentrancy would happen after the shares are burned
    # and after the assets are transferred, which is a valid state.
    erc20._burn(owner, shares)

    # To deal with (potentially) non-compliant ERC-20 tokens that do have
    # no return value, we use the kwarg `default_return_value` for external
    # calls. This function was introduced in Vyper version `0.3.4`. For more
    # details see:
    # - https://github.com/vyperlang/vyper/pull/2839,
    # - https://github.com/vyperlang/vyper/issues/2812,
    # - https://medium.com/coinmonks/missing-return-value-bug-at-least-130-tokens-affected-d67bf08521ca.

    # It is important to note that an external call via interface casting
    # always performs an external code size check on the target address unless
    # you add the kwarg `skip_contract_check=True`. If the check fails (i.e.
    # the target address is an EOA), the call reverts.
    assert extcall _ASSET.transfer(
        receiver, assets, default_return_value=True
    ), "erc4626: transfer operation did not succeed"
    log IERC4626.Withdraw(sender=sender, receiver=receiver, owner=owner, assets=assets, shares=shares)
