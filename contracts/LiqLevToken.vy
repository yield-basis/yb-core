# @version 0.4.0
"""
@title LiqLevToken
@notice AMM for leveraging 2-token liquidity
@author Michael Egorov
@license Copyright (c) 2024
"""

interface IERC20:
    def decimals() -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable

interface LevAMM:
    def _deposit(d_collateral: uint256, d_debt: uint256, min_invariant_change: uint256) -> uint256: nonpayable
    def _withdraw(invariant_change: uint256, min_collateral_return: uint256, max_debt_return: uint256) -> uint256[2]: nonpayable
    def invariant_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> uint256[2]: view
    def fee() -> uint256: view
    def value_oracle() -> uint256: view
    def value_oracle_for(collateral: uint256, debt: uint256) -> uint256: view

interface CurveCryptoPool:
    def add_liquidity(amounts: uint256[2], min_mint_amount: uint256, receiver: address) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: uint256[2], receiver: address) -> uint256[2]: nonpayable
    def lp_price() -> uint256: view
    def get_virtual_price() -> uint256: view
    def price_oracle() -> uint256: view
    def decimals() -> uint256: view
    def mid_fee() -> uint256: view
    def totalSupply() -> uint256: view
    def coins(i: uint256) -> address: view
    def calc_token_amount(amounts: uint256[2], deposit: bool) -> uint256: view
    def balances(i: uint256) -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable

# ERC20 events

event Approval:
    owner: indexed(address)
    spender: indexed(address)
    value: uint256

event Transfer:
    sender: indexed(address)
    receiver: indexed(address)
    value: uint256


# ERC4626 events

event Deposit:
    sender: indexed(address)
    owner: indexed(address)
    assets: uint256
    shares: uint256

event Withdraw:
    sender: indexed(address)
    receiver: indexed(address)
    owner: indexed(address)
    assets: uint256
    shares: uint256


COLLATERAL: public(immutable(CurveCryptoPool))  # Liquidity like LP(TBTC/crvUSD)
STABLECOIN: public(immutable(IERC20))  # For example, crvUSD
DEPOSITED_TOKEN: public(immutable(IERC20))  # For example, TBTC
DEPOSITED_TOKEN_PRECISION: immutable(uint256)

admin: public(address)
amm: public(LevAMM)

event SetAdmin:
    admin: address


allowance: public(HashMap[address, HashMap[address, uint256]])
balanceOf: public(HashMap[address, uint256])
totalSupply: public(uint256)


@deploy
def __init__(deposited_token: IERC20, stablecoin: IERC20, collateral: CurveCryptoPool,
             admin: address):
    """
    @notice Initializer (can be performed by an EOA deployer or a factory)
    @param deposited_token Token which gets deposited. Can be collateral or can be not
    @param stablecoin Stablecoin which gets "granted" to this contract to use for loans. Has to be 18 decimals
    @param collateral Collateral token
    @param admin Admin which can set callbacks, stablecoin allocator and fee. Sensitive!
    """
    # Example:
    # deposit_token = WBTC
    # stablecoin = crvUSD
    # collateral = WBTC LP

    STABLECOIN = stablecoin
    COLLATERAL = collateral
    DEPOSITED_TOKEN = deposited_token
    DEPOSITED_TOKEN_PRECISION = 10**(18 - staticcall deposited_token.decimals())
    self.admin = admin
    assert extcall deposited_token.approve(collateral.address, max_value(uint256), default_return_value=True)
    assert extcall stablecoin.approve(collateral.address, max_value(uint256), default_return_value=True)
    assert staticcall collateral.coins(0) == stablecoin.address
    assert staticcall collateral.coins(1) == deposited_token.address


@internal
@pure
def sqrt(arg: uint256) -> uint256:
    return isqrt(arg)


@external
@nonreentrant
def deposit(assets: uint256, receiver: address = msg.sender) -> uint256:
    """
    @notice Simple method to deposit assets (e.g. like BTC) to receive shares (e.g. like yield-bearing BTC) - auto slippage
    @param assets Amount of assets to deposit
    @param receiver Receiver of the shares who is optional. If not specified - receiver is the sender
    """
    amm: LevAMM = self.amm
    # p_asset: uint256 = COLLATERAL.price_oracle()
    # value_in_amm: uint256 = self.amm.value_oracle()
    # total_btc = value_in_amm / p_asset - and nonmanipulatable

    p_asset: uint256 = staticcall COLLATERAL.price_oracle()
    loan_amount: uint256 = assets * DEPOSITED_TOKEN_PRECISION * p_asset // 10**18

    # For calculating min shares
    vprice: uint256 = staticcall COLLATERAL.get_virtual_price()
    min_lp: uint256 = self.sqrt(assets * DEPOSITED_TOKEN_PRECISION * loan_amount) * 10**18 // vprice
    min_lp = min_lp * (10**10 - staticcall COLLATERAL.mid_fee()) // 10**10  # Sandwiches are unprofitable for this %

    collateral_shares: uint256 = extcall COLLATERAL.add_liquidity([loan_amount, assets], min_lp, amm.address)

    # Process deposit
    amm_initial_value: uint256 = staticcall amm.value_oracle()
    extcall amm._deposit(collateral_shares, loan_amount, 0)
    d_amm_value: uint256 = staticcall amm.value_oracle() - amm_initial_value

    # loan amount is exactly assets * price
    assert d_amm_value >= loan_amount * (10**18 - staticcall amm.fee()) // 10**18, "Slippage in LevAMM"

    to_mint: uint256 = assets * DEPOSITED_TOKEN_PRECISION
    if self.totalSupply > 0:
        to_mint = self.totalSupply * d_amm_value // amm_initial_value
    self._mint(receiver, to_mint)

    log Deposit(msg.sender, receiver, assets, to_mint)
    return to_mint


@external
@view
@nonreentrant
def expected_tokens(assets: uint256, debt: uint256) -> uint256:
    """
    @notice Returns the amount of shares which can be obtained upon depositing assets, including slippage
    @param assets Amount of crypto to deposit
    @param debt Amount of stables to borrow for MMing (approx same value as crypto)
    """
    lp_tokens: uint256 = staticcall COLLATERAL.calc_token_amount([assets, debt], True)
    supply: uint256 = self.totalSupply
    if supply > 0:
        invariants: uint256[2] = staticcall self.amm.invariant_change(lp_tokens, debt, True)
        return supply * invariants[1] // invariants[0] - supply
    else:
        return staticcall self.amm.value_oracle_for(lp_tokens, debt)


@external
@nonreentrant
def set_amm(amm: LevAMM):
    assert msg.sender == self.admin, "Access"
    assert self.amm == empty(LevAMM), "Already set"
    self.amm = amm


@external
@nonreentrant
def set_admin(new_admin: address):
    assert msg.sender == self.admin, "Access"
    self.admin = new_admin
    log SetAdmin(new_admin)


# ERC20 methods

@internal
def _approve(_owner: address, _spender: address, _value: uint256):
    self.allowance[_owner][_spender] = _value

    log Approval(_owner, _spender, _value)


@internal
def _burn(_from: address, _value: uint256):
    self.balanceOf[_from] -= _value
    self.totalSupply -= _value

    log Transfer(_from, empty(address), _value)


@internal
def _mint(_to: address, _value: uint256):
    self.balanceOf[_to] += _value
    self.totalSupply += _value

    log Transfer(empty(address), _to, _value)


@internal
def _transfer(_from: address, _to: address, _value: uint256):
    assert _to not in [self, empty(address)]

    self.balanceOf[_from] -= _value
    self.balanceOf[_to] += _value

    log Transfer(_from, _to, _value)


@external
def transferFrom(_from: address, _to: address, _value: uint256) -> bool:
    """
    @notice Transfer tokens from one account to another.
    @dev The caller needs to have an allowance from account `_from` greater than or
        equal to the value being transferred. An allowance equal to the uint256 type's
        maximum, is considered infinite and does not decrease.
    @param _from The account which tokens will be spent from.
    @param _to The account which tokens will be sent to.
    @param _value The amount of tokens to be transferred.
    """
    allowance: uint256 = self.allowance[_from][msg.sender]
    if allowance != max_value(uint256):
        self._approve(_from, msg.sender, allowance - _value)

    self._transfer(_from, _to, _value)
    return True


@external
def transfer(_to: address, _value: uint256) -> bool:
    """
    @notice Transfer tokens to `_to`.
    @param _to The account to transfer tokens to.
    @param _value The amount of tokens to transfer.
    """
    self._transfer(msg.sender, _to, _value)
    return True


@external
def approve(_spender: address, _value: uint256) -> bool:
    """
    @notice Allow `_spender` to transfer up to `_value` amount of tokens from the caller's account.
    @dev Non-zero to non-zero approvals are allowed, but should be used cautiously. The methods
        increaseAllowance + decreaseAllowance are available to prevent any front-running that
        may occur.
    @param _spender The account permitted to spend up to `_value` amount of caller's funds.
    @param _value The amount of tokens `_spender` is allowed to spend.
    """
    self._approve(msg.sender, _spender, _value)
    return True


@external
def increaseAllowance(_spender: address, _add_value: uint256) -> bool:
    """
    @notice Increase the allowance granted to `_spender`.
    @dev This function will never overflow, and instead will bound
        allowance to MAX_UINT256. This has the potential to grant an
        infinite approval.
    @param _spender The account to increase the allowance of.
    @param _add_value The amount to increase the allowance by.
    """
    cached_allowance: uint256 = self.allowance[msg.sender][_spender]
    allowance: uint256 = unsafe_add(cached_allowance, _add_value)

    # check for an overflow
    if allowance < cached_allowance:
        allowance = max_value(uint256)

    if allowance != cached_allowance:
        self._approve(msg.sender, _spender, allowance)

    return True


@external
def decreaseAllowance(_spender: address, _sub_value: uint256) -> bool:
    """
    @notice Decrease the allowance granted to `_spender`.
    @dev This function will never underflow, and instead will bound
        allowance to 0.
    @param _spender The account to decrease the allowance of.
    @param _sub_value The amount to decrease the allowance by.
    """
    cached_allowance: uint256 = self.allowance[msg.sender][_spender]
    allowance: uint256 = unsafe_sub(cached_allowance, _sub_value)

    # check for an underflow
    if cached_allowance < allowance:
        allowance = 0

    if allowance != cached_allowance:
        self._approve(msg.sender, _spender, allowance)

    return True
