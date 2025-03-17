# @version 0.4.1
"""
@title LT
@notice Implementation of leveraged liquidity for Yield Basis
@author Scientia Spectra AG
@license Copyright (c) 2025
"""

interface IERC20:
    def decimals() -> uint256: view
    def balanceOf(_user: address) -> uint256: view
    def approve(_to: address, _value: uint256) -> bool: nonpayable
    def transfer(_to: address, _value: uint256) -> bool: nonpayable
    def transferFrom(_from: address, _to: address, _value: uint256) -> bool: nonpayable

interface LevAMM:
    def _deposit(d_collateral: uint256, d_debt: uint256) -> ValueChange: nonpayable
    def _withdraw(frac: uint256) -> Pair: nonpayable
    def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> ValueChange: view
    def fee() -> uint256: view
    def value_oracle() -> OraclizedValue: view
    def get_state() -> AMMState: view
    def value_oracle_for(collateral: uint256, debt: uint256) -> OraclizedValue: view
    def set_rate(rate: uint256) -> uint256: nonpayable
    def collect_fees() -> uint256: nonpayable
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def max_debt() -> uint256: view

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
    def donate(amounts: uint256[2], min_amount: uint256): nonpayable
    def remove_liquidity_fixed_out(token_amount: uint256, i: uint256, amount_i: uint256, min_amount_j: uint256) -> uint256: nonpayable
    def calc_withdraw_fixed_out(token_amount: uint256, i: uint256, amount_i: uint256) -> uint256: view

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view
    def AGG() -> address: view


struct AMMState:
    collateral: uint256
    debt: uint256
    x0: uint256

struct Pair:
    collateral: uint256
    debt: uint256

struct ValueChange:
    p_o: uint256
    value_before: uint256
    value_after: uint256

struct OraclizedValue:
    p_o: uint256
    value: uint256

struct LiquidityValues:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256

struct LiquidityValuesOut:
    admin: int256  # Can be negative
    total: uint256
    ideal_staked: uint256
    staked: uint256
    staked_tokens: uint256
    supply_tokens: uint256


event SetStaker:
    staker: indexed(address)

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


event SetAdmin:
    admin: address


COLLATERAL: public(immutable(CurveCryptoPool))  # Liquidity like LP(TBTC/crvUSD)
STABLECOIN: public(immutable(IERC20))  # For example, crvUSD
DEPOSITED_TOKEN: public(immutable(IERC20))  # For example, TBTC
DEPOSITED_TOKEN_PRECISION: immutable(uint256)

FEE_CLAIM_DISCOUNT: constant(uint256) = 10**16

admin: public(address)
amm: public(LevAMM)
agg: public(PriceOracle)

staker: public(address)

min_admin_fee: public(uint256)

liquidity: public(LiquidityValues)

allowance: public(HashMap[address, HashMap[address, uint256]])
balanceOf: public(HashMap[address, uint256])
totalSupply: public(uint256)

stablecoin_allocations: public(HashMap[address, uint256])
stablecoin_allocated: public(HashMap[address, uint256])


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


@internal
@view
def _price_oracle() -> uint256:
    return staticcall COLLATERAL.price_oracle() * staticcall self.agg.price() // 10**18


@internal
def _price_oracle_w() -> uint256:
    return staticcall COLLATERAL.price_oracle() * extcall self.agg.price_w() // 10**18


@internal
@view
def _calculate_values(p_o: uint256) -> LiquidityValuesOut:
    prev: LiquidityValues = self.liquidity
    staker: address = self.staker
    staked: int256 = 0
    if staker != empty(address):
        staked = convert(self.balanceOf[self.staker], int256)
    supply: int256 = convert(self.totalSupply, int256)

    f_a: int256 = convert(
        10**18 - (10**18 - self.min_admin_fee) * self.sqrt(convert(10**36 - staked * 10**36 // supply, uint256)) // 10**18,
        int256)

    cur_value: int256 = convert((staticcall self.amm.value_oracle()).value * 10**18 // p_o, int256)
    prev_value: int256 = convert(prev.total, int256)
    value_change: int256 = cur_value - (prev_value + prev.admin)

    v_st: int256 = convert(prev.staked, int256)
    v_st_ideal: int256 = convert(prev.ideal_staked, int256)
    # ideal_staked is set when some tokens are transferred to staker address

    dv_use: int256 = value_change * (10**18 - f_a) // 10**18
    prev.admin += (value_change - dv_use)

    dv_s: int256 = dv_use * staked // supply
    if dv_use > 0:
        dv_s = min(dv_s, max(v_st_ideal - v_st, 0))

    new_total_value: int256 = prev_value + dv_use
    new_staked_value: int256 = v_st + dv_s

    # Solution of:
    # staked - token_reduction       new_staked_value
    # -------------------------  =  -------------------
    # supply - token_reduction         new_token_value
    token_reduction: int256 = unsafe_div(staked * new_total_value - new_staked_value * supply, new_total_value - new_staked_value)
    # token_reduction = 0 if nothing is staked
    # XXX need to consider situation when denominator is very close to zero

    # Supply changes each time:
    # value split reduces the amount of staked tokens (but not others),
    # and this also reduces the supply of LP tokens

    return LiquidityValuesOut(
        admin=prev.admin,
        total=convert(new_total_value, uint256),
        ideal_staked=prev.ideal_staked,
        staked=convert(new_staked_value, uint256),
        staked_tokens=convert(staked - token_reduction, uint256),
        supply_tokens=convert(supply - token_reduction, uint256)
    )


@external
@view
@nonreentrant
def preview_deposit(assets: uint256, debt: uint256 = max_value(uint256)) -> uint256:
    """
    @notice Returns the amount of shares which can be obtained upon depositing assets, including slippage
    @param assets Amount of crypto to deposit
    @param debt Amount of stables to borrow for MMing (approx same value as crypto) or best guess if max_value
    """
    lp_tokens: uint256 = staticcall COLLATERAL.calc_token_amount([debt, assets], True)
    supply: uint256 = self.totalSupply
    p_o: uint256 = self._price_oracle()
    if supply > 0:
        liquidity: LiquidityValuesOut = self._calculate_values(p_o)
        v: ValueChange = staticcall self.amm.value_change(lp_tokens, debt, True)
        # Liquidity contains admin fees, so we need to subtract
        # If admin fees are negative - we get LESS LP tokens
        # value_before = v.value_before - liquidity.admin = total
        value_after: uint256 = convert(convert(v.value_after * 10**18 // p_o, int256) - liquidity.admin, uint256)
        return liquidity.supply_tokens * value_after // liquidity.total - liquidity.supply_tokens

    v: OraclizedValue = staticcall self.amm.value_oracle_for(lp_tokens, debt)
    return v.value * 10**18 // p_o


@external
@view
@nonreentrant
def preview_withdraw(tokens: uint256) -> uint256:
    """
    @notice Returns the amount of assets which can be obtained upon withdrawing from tokens
    """
    v: LiquidityValuesOut = self._calculate_values(self._price_oracle())
    state: AMMState = staticcall self.amm.get_state()
    # Total does NOT include uncollected admin fees
    # however we account only for positive admin balance. This "socializes" losses if they happen
    admin_balance: uint256 = convert(max(v.admin, 0), uint256)
    withdrawn_lp: uint256 = state.collateral * v.total // (v.total + admin_balance) * tokens // v.supply_tokens
    withdrawn_debt: uint256 = state.debt * v.total // (v.total + admin_balance) * tokens // v.supply_tokens
    return staticcall COLLATERAL.calc_withdraw_fixed_out(withdrawn_lp, 0, withdrawn_debt)


@external
@nonreentrant
def deposit(assets: uint256, debt: uint256, min_shares: uint256, receiver: address = msg.sender) -> uint256:
    """
    @notice Method to deposit assets (e.g. like BTC) to receive shares (e.g. like yield-bearing BTC)
    @param assets Amount of assets to deposit
    @param debt Amount of debt for AMM to take (approximately BTC * btc_price)
    @param min_shares Minimal amount of shares to receive (important to calculate to exclude sandwich attacks)
    @param receiver Receiver of the shares who is optional. If not specified - receiver is the sender
    """
    amm: LevAMM = self.amm
    assert extcall STABLECOIN.transferFrom(amm.address, self, debt)
    assert extcall DEPOSITED_TOKEN.transferFrom(msg.sender, self, assets)
    lp_tokens: uint256 = extcall COLLATERAL.add_liquidity([debt, assets], 0, amm.address)
    p_o: uint256 = self._price_oracle_w()

    supply: uint256 = self.totalSupply
    shares: uint256 = 0

    liquidity_values: LiquidityValuesOut = empty(LiquidityValuesOut)
    if supply > 0:
        liquidity_values = self._calculate_values(p_o)

    v: ValueChange = extcall amm._deposit(lp_tokens, debt)
    value_after: uint256 = v.value_after * 10**18 // p_o

    # Value is measured in USD
    # Do not allow value to become larger than HALF of the available stablecoins after the deposit
    # If value becomes too large - we don't allow to deposit more to have a buffer when the price rises
    assert staticcall amm.max_debt() // 2 >= v.value_after, "Debt too high"

    if supply > 0:
        supply = liquidity_values.supply_tokens
        self.liquidity.admin = liquidity_values.admin
        value_before: uint256 = liquidity_values.total
        value_after = convert(convert(value_after, int256) - liquidity_values.admin, uint256)
        self.liquidity.total = value_after
        self.liquidity.staked = liquidity_values.staked
        self.totalSupply = liquidity_values.supply_tokens  # will be increased by mint
        staker: address = self.staker
        if staker != empty(address):
            self.balanceOf[staker] = liquidity_values.staked_tokens
        # ideal_staked is only changed when we transfer coins to staker
        shares = supply * value_after // value_before - supply

    else:
        # Initial value/shares ratio is EXACTLY 1.0 in collateral units
        # Value is measured in USD
        shares = value_after
        # self.liquidity.admin is 0 at start but can be rolled over if everything was withdrawn
        self.liquidity.ideal_staked = 0  # Likely already 0 since supply was 0
        self.liquidity.staked = 0        # Same: nothing staked when supply is 0
        self.liquidity.total = shares    # 1 share = 1 crypto at first deposit
        self.liquidity.admin = 0         # if we had admin fees - give them to the first depositor; simpler to handle

    assert shares >= min_shares, "Slippage"

    self._mint(receiver, shares)
    log Deposit(sender=msg.sender, owner=receiver, assets=assets, shares=shares)
    return shares


@external
@nonreentrant
def withdraw(shares: uint256, min_assets: uint256, receiver: address = msg.sender) -> uint256:
    """
    @notice Method to withdraw assets (e.g. like BTC) by spending shares (e.g. like yield-bearing BTC)
    @param shares Shares to withdraw
    @param min_assets Minimal amount of assets to receive (important to calculate to exclude sandwich attacks)
    @param receiver Receiver of the shares who is optional. If not specified - receiver is the sender
    """
    assert shares > 0, "Withdrawing nothing"

    amm: LevAMM = self.amm
    liquidity_values: LiquidityValuesOut = self._calculate_values(self._price_oracle_w())
    supply: uint256 = liquidity_values.supply_tokens
    self.liquidity.admin = liquidity_values.admin
    self.liquidity.total = liquidity_values.total
    self.liquidity.staked = liquidity_values.staked
    self.totalSupply = supply
    staker: address = self.staker
    if staker != empty(address):
        self.balanceOf[staker] = liquidity_values.staked_tokens
    state: AMMState = staticcall amm.get_state()

    admin_balance: uint256 = convert(max(liquidity_values.admin, 0), uint256)

    withdrawn: Pair = extcall amm._withdraw(10**18 * liquidity_values.total // (liquidity_values.total + admin_balance) * shares // supply)
    assert extcall COLLATERAL.transferFrom(amm.address, self, withdrawn.collateral)
    crypto_received: uint256 = extcall COLLATERAL.remove_liquidity_fixed_out(withdrawn.collateral, 0, withdrawn.debt, 0)

    self._burn(msg.sender, shares)  # Changes self.totalSupply
    self.liquidity.total = liquidity_values.total * (supply - shares) // supply
    if liquidity_values.admin < 0:
        # If admin fees are negative - we are skipping them, so reduce proportionally
        self.liquidity.admin = liquidity_values.admin * convert(supply - shares, int256) // convert(supply, int256)
    assert crypto_received >= min_assets, "Slippage"
    assert extcall STABLECOIN.transfer(amm.address, withdrawn.debt)
    assert extcall DEPOSITED_TOKEN.transfer(receiver, crypto_received)

    log Withdraw(sender=msg.sender, receiver=receiver, owner=msg.sender, assets=crypto_received, shares=shares)
    return crypto_received


@external
@view
def pricePerShare() -> uint256:
    """
    Non-manipulatable "fair price per share" oracle
    """
    v: LiquidityValuesOut = self._calculate_values(self._price_oracle())
    return v.total * 10**18 // v.supply_tokens


@external
@nonreentrant
def set_amm(amm: LevAMM):
    assert msg.sender == self.admin, "Access"
    assert self.amm == empty(LevAMM), "Already set"
    self.amm = amm
    self.agg = PriceOracle(staticcall (staticcall amm.PRICE_ORACLE_CONTRACT()).AGG())


@external
@nonreentrant
def set_admin(new_admin: address):
    assert msg.sender == self.admin, "Access"
    self.admin = new_admin
    log SetAdmin(admin=new_admin)


@external
@nonreentrant
def set_rate(rate: uint256):
    assert msg.sender == self.admin, "Access"
    extcall self.amm.set_rate(rate)


@external
@nonreentrant
def allocate_stablecoins(allocator: address, limit: uint256 = max_value(uint256)):
    """
    @notice This method has to be used once this contract has received allocation of stablecoins
    @param allocator Address of the allocator to provide stables for us
    @param limit Limit to allocate for this pool from this allocator. Max uint256 = do not change
    """
    assert msg.sender == self.admin, "Access"

    allocation: uint256 = limit
    allocated: uint256 = self.stablecoin_allocated[allocator]
    if limit == max_value(uint256):
        allocation = self.stablecoin_allocations[allocator]
    else:
        self.stablecoin_allocations[allocator] = limit

    if allocation > allocated:
        # Assume that allocator has everything
        extcall STABLECOIN.transferFrom(allocator, self.amm.address, allocation - allocated)
        self.stablecoin_allocated[allocator] = allocation

    elif allocation < allocated:
        to_transfer: uint256 = min(allocated - allocation, staticcall STABLECOIN.balanceOf(self.amm.address))
        allocated -= to_transfer
        extcall STABLECOIN.transferFrom(self.amm.address, allocator, to_transfer)
        self.stablecoin_allocated[allocator] = allocated


@external
@nonreentrant
def distrubute_borrower_fees(discount: uint256 = FEE_CLAIM_DISCOUNT):  # This will JUST donate to the crypto pool
    if discount > FEE_CLAIM_DISCOUNT:
        assert msg.sender == self.admin, "Access"
    extcall self.amm.collect_fees()
    amount: uint256 = staticcall STABLECOIN.balanceOf(self)
    # We price to the stablecoin we use, not the aggregated USD here, and this is correct
    min_amount: uint256 = (10**18 - discount) * amount // staticcall COLLATERAL.lp_price()
    extcall COLLATERAL.donate([amount, 0], min_amount)


@external
@nonreentrant
def set_staker(staker: address):
    assert msg.sender == self.admin, "Access"
    self.staker = staker
    log SetStaker(staker=staker)


# ERC20 methods

@internal
def _approve(_owner: address, _spender: address, _value: uint256):
    self.allowance[_owner][_spender] = _value

    log Approval(owner=_owner, spender=_spender, value=_value)


@internal
def _burn(_from: address, _value: uint256):
    self.balanceOf[_from] -= _value
    self.totalSupply -= _value

    log Transfer(sender=_from, receiver=empty(address), value=_value)


@internal
def _mint(_to: address, _value: uint256):
    self.balanceOf[_to] += _value
    self.totalSupply += _value

    log Transfer(sender=empty(address), receiver=_to, value=_value)


@internal
def _transfer(_from: address, _to: address, _value: uint256):
    assert _to not in [self, empty(address)]

    staker: address = self.staker
    if staker != empty(address) and staker in [_from, _to]:
        assert _from != _to
        liquidity: LiquidityValuesOut = self._calculate_values(self._price_oracle_w())
        self.liquidity.admin = liquidity.admin
        self.liquidity.total = liquidity.total
        self.totalSupply = liquidity.supply_tokens
        self.balanceOf[staker] = liquidity.staked_tokens
        if _from == staker:
            # Reduce the staked part
            liquidity.staked -= liquidity.total * _value // liquidity.supply_tokens
            liquidity.ideal_staked = liquidity.ideal_staked * (liquidity.staked_tokens - _value) // liquidity.staked_tokens
        elif _to == staker:
            # Increase the staked part
            d_staked_value: uint256 = liquidity.total * _value // liquidity.supply_tokens
            liquidity.staked += d_staked_value
            if liquidity.staked_tokens > 10**10:
                liquidity.ideal_staked = liquidity.ideal_staked * (liquidity.staked_tokens + _value) // liquidity.staked_tokens
            else:
                # To exclude division by zero and numerical noise errors
                liquidity.ideal_staked += d_staked_value
        self.liquidity.staked = liquidity.staked
        self.liquidity.ideal_staked = liquidity.ideal_staked

    self.balanceOf[_from] -= _value
    self.balanceOf[_to] += _value

    log Transfer(sender=_from, receiver=_to, value=_value)


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
