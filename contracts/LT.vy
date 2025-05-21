# @version 0.4.1
"""
@title LT
@notice Implementation of leveraged liquidity for Yield Basis
@author Scientia Spectra AG
@license Copyright (c) 2025
"""
from ethereum.ercs import IERC20
from snekmate.utils import math

implements: IERC20


interface IERC20Detailed:
    def decimals() -> uint256: view

interface IERC20Slice:
    def symbol() -> String[29]: view

interface LevAMM:
    def _deposit(d_collateral: uint256, d_debt: uint256) -> ValueChange: nonpayable
    def _withdraw(frac: uint256) -> Pair: nonpayable
    def value_change(collateral_amount: uint256, borrowed_amount: uint256, is_deposit: bool) -> ValueChange: view
    def fee() -> uint256: view
    def value_oracle() -> OraclizedValue: view
    def get_state() -> AMMState: view
    def get_debt() -> uint256: view
    def collateral_amount() -> uint256: view
    def value_oracle_for(collateral: uint256, debt: uint256) -> OraclizedValue: view
    def set_rate(rate: uint256) -> uint256: nonpayable
    def collect_fees() -> uint256: nonpayable
    def PRICE_ORACLE_CONTRACT() -> PriceOracle: view
    def max_debt() -> uint256: view
    def COLLATERAL() -> address: view
    def STABLECOIN() -> address: view
    def LT_CONTRACT() -> address: view
    def set_killed(is_killed: bool): nonpayable
    def check_nonreentrant(): nonpayable
    def is_killed() -> bool: view
    def set_fee(fee: uint256): nonpayable

interface CurveCryptoPool:
    def add_liquidity(amounts: uint256[2], min_mint_amount: uint256, receiver: address, donation: bool) -> uint256: nonpayable
    def remove_liquidity(amount: uint256, min_amounts: uint256[2]) -> uint256[2]: nonpayable
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
    def remove_liquidity_fixed_out(token_amount: uint256, i: uint256, amount_i: uint256, min_amount_j: uint256) -> uint256: nonpayable
    def calc_withdraw_fixed_out(token_amount: uint256, i: uint256, amount_i: uint256) -> uint256: view

interface PriceOracle:
    def price_w() -> uint256: nonpayable
    def price() -> uint256: view
    def AGG() -> address: view

interface Factory:
    def admin() -> address: view
    def emergency_admin() -> address: view
    def fee_receiver() -> address: view
    def min_admin_fee() -> uint256: view


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
    token_reduction: int256


event SetStaker:
    staker: indexed(address)


event WithdrawAdminFees:
    receiver: address
    amount: uint256

event AllocateStablecoins:
    allocator: indexed(address)
    stablecoin_allocation: uint256
    stablecoin_allocated: uint256

event DistributeBorrowerFees:
    sender: indexed(address)
    amount: uint256
    min_amount: uint256
    discount: uint256


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


CRYPTOPOOL: public(immutable(CurveCryptoPool))  # Liquidity like LP(TBTC/crvUSD)
STABLECOIN: public(immutable(IERC20))  # For example, crvUSD
ASSET_TOKEN: public(immutable(IERC20))  # For example, TBTC

CRYPTOPOOL_N_COINS: constant(uint256) = 2
FEE_CLAIM_DISCOUNT: constant(uint256) = 10**16
MIN_SHARE_REMAINDER: constant(uint256) = 10**6  # We leave at least this much of shares if > 0
SQRT_MIN_UNSTAKED_FRACTION: constant(int256) = 10**14  # == 1e-4, avoiding infinite APR and 0/0 errors

admin: public(address)
amm: public(LevAMM)
agg: public(PriceOracle)

staker: public(address)

liquidity: public(LiquidityValues)

allowance: public(HashMap[address, HashMap[address, uint256]])
balanceOf: public(HashMap[address, uint256])
totalSupply: public(uint256)
decimals: public(constant(uint8)) = 18

stablecoin_allocation: public(uint256)
stablecoin_allocated: public(uint256)


@deploy
def __init__(asset_token: IERC20, stablecoin: IERC20, cryptopool: CurveCryptoPool,
             admin: address):
    """
    @notice Initializer (can be performed by an EOA deployer or a factory)
    @param asset_token Token which gets deposited. Can be collateral or can be not
    @param stablecoin Stablecoin which gets "granted" to this contract to use for loans. Has to be 18 decimals
    @param cryptopool Cryptopool LP collateral token
    @param admin Admin which can set callbacks, stablecoin allocator and fee. Sensitive!
    """
    # Example:
    # asset_token = WBTC
    # stablecoin = crvUSD
    # cryptopool = WBTC LP

    STABLECOIN = stablecoin
    CRYPTOPOOL = cryptopool
    ASSET_TOKEN = asset_token
    self.admin = admin
    assert extcall asset_token.approve(cryptopool.address, max_value(uint256), default_return_value=True)
    assert extcall stablecoin.approve(cryptopool.address, max_value(uint256), default_return_value=True)
    assert staticcall cryptopool.coins(0) == stablecoin.address
    assert staticcall cryptopool.coins(1) == asset_token.address

    # Twocrypto has no N_COINS public, so we check that coins(2) reverts
    success: bool = False
    res: Bytes[32] = empty(Bytes[32])
    success, res = raw_call(
        cryptopool.address,
        abi_encode(CRYPTOPOOL_N_COINS, method_id=method_id("coins(uint256)")),
        max_outsize=32,
        is_static_call=True,
        revert_on_failure=False)
    assert not success, "N>2"


@internal
@pure
def sqrt(arg: uint256) -> uint256:
    return isqrt(arg)


@internal
@view
def _check_admin():
    admin: address = self.admin
    if admin.is_contract:
        assert msg.sender == admin or msg.sender == staticcall Factory(admin).admin(), "Access"
    else:
        assert msg.sender == admin, "Access"


@internal
@view
def _price_oracle() -> uint256:
    return staticcall CRYPTOPOOL.price_oracle() * staticcall self.agg.price() // 10**18


@internal
def _price_oracle_w() -> uint256:
    return staticcall CRYPTOPOOL.price_oracle() * extcall self.agg.price_w() // 10**18


@internal
@view
def _min_admin_fee() -> uint256:
    admin: address = self.admin
    if admin.is_contract:
        return staticcall Factory(admin).min_admin_fee()
    else:
        return 0


@external
@view
def min_admin_fee() -> uint256:
    return self._min_admin_fee()


@internal
@pure
def mul_div_signed(x: int256, y: int256, denominator: int256) -> int256:
    if denominator == 0:
        return 0

    value: int256 = convert(
        math._mul_div(
            convert(abs(x), uint256),
            convert(abs(y), uint256),
            convert(abs(denominator), uint256),
            False),
        int256)

    if ((x < 0) != (y < 0)) != (denominator < 0):
        value = -value

    return value


@internal
@view
def _calculate_values(p_o: uint256) -> LiquidityValuesOut:
    prev: LiquidityValues = self.liquidity
    staker: address = self.staker
    staked: int256 = 0
    if staker != empty(address):
        staked = convert(self.balanceOf[self.staker], int256)
    supply: int256 = convert(self.totalSupply, int256)
    # staked is guaranteed to be <= supply

    f_a: int256 = convert(
        10**18 - (10**18 - self._min_admin_fee()) * self.sqrt(convert(10**36 - staked * 10**36 // supply, uint256)) // 10**18,
        int256)

    cur_value: int256 = convert((staticcall self.amm.value_oracle()).value * 10**18 // p_o, int256)
    prev_value: int256 = convert(prev.total, int256)
    value_change: int256 = cur_value - (prev_value + prev.admin)

    v_st: int256 = convert(prev.staked, int256)
    v_st_ideal: int256 = convert(prev.ideal_staked, int256)
    # ideal_staked is set when some tokens are transferred to staker address

    # _36 postifix is to emphasize that the value is 1e36-based, not 1e18, for type tracking purposes

    dv_use_36: int256 = value_change * (10**18 - f_a)
    prev.admin += (value_change - dv_use_36 // 10**18)

    # dv_s is guaranteed to be <= dv_use
    # if staked < supply (not exactly 100.0% staked) - dv_s is strictly < dv_use
    dv_s_36: int256 = self.mul_div_signed(dv_use_36, staked, supply)
    if dv_use_36 > 0:
        dv_s_36 = min(dv_s_36, max(v_st_ideal - v_st, 0) * 10**18)

    # new_staked_value is guaranteed to be <= new_total_value
    new_total_value_36: int256 = max(prev_value * 10**18 + dv_use_36, 0)
    new_staked_value_36: int256 = max(v_st * 10**18 + dv_s_36, 0)

    # Solution of:
    # staked - token_reduction       new_staked_value
    # -------------------------  =  -------------------
    # supply - token_reduction         new_total_value
    #
    # the result:
    #                      new_total_value * staked - new_staked_value * supply
    # token_reduction  =  ------------------------------------------------------
    #                               new_total_value - new_staked_value
    #
    # When eps = (supply - staked) / supply << 1, it comes down to:
    # token_reduction = value_change / total_value * (1.0 - min_admin_fee) / sqrt(eps) * supply
    # So when eps < 1e-8 - we'll limit token_reduction

    # If denominator is 0 -> token_reduction = 0 (not a revert)

    token_reduction: int256 = new_total_value_36 - new_staked_value_36  # Denominator
    token_reduction = self.mul_div_signed(new_total_value_36, staked, token_reduction) - self.mul_div_signed(new_staked_value_36, supply, token_reduction)

    max_token_reduction: int256 = abs(value_change * supply // (prev_value + value_change + 1) * (10**18 - f_a) // SQRT_MIN_UNSTAKED_FRACTION)

    # let's leave at least 1 LP token for staked and for total
    if staked > 0:
        token_reduction = min(token_reduction, staked - 1)
    if supply > 0:
        token_reduction = min(token_reduction, supply - 1)
    # But most likely it's this condition to apply
    if token_reduction >= 0:
        token_reduction = min(token_reduction, max_token_reduction)
    else:
        token_reduction = max(token_reduction, -max_token_reduction)
    # And don't allow negatives if denominator was too small
    if new_total_value_36 - new_staked_value_36 < 10**4 * 10**18:
        token_reduction = max(token_reduction, 0)

    # Supply changes each time:
    # value split reduces the amount of staked tokens (but not others),
    # and this also reduces the supply of LP tokens

    return LiquidityValuesOut(
        admin=prev.admin,
        total=convert(new_total_value_36 // 10**18, uint256),
        ideal_staked=prev.ideal_staked,
        staked=convert(new_staked_value_36 // 10**18, uint256),
        staked_tokens=convert(staked - token_reduction, uint256),
        supply_tokens=convert(supply - token_reduction, uint256),
        token_reduction=token_reduction
    )


@internal
def _log_token_reduction(staker: address, token_reduction: int256):
    if token_reduction < 0:
        log IERC20.Transfer(sender=empty(address), receiver=staker, value=convert(-token_reduction, uint256))
    if token_reduction > 0:
        log IERC20.Transfer(sender=staker, receiver=empty(address), value=convert(token_reduction, uint256))


@external
@view
@nonreentrant
def preview_deposit(assets: uint256, debt: uint256) -> uint256:
    """
    @notice Returns the amount of shares which can be obtained upon depositing assets, including slippage
    @param assets Amount of crypto to deposit
    @param debt Amount of stables to borrow for MMing (approx same value as crypto)
    """
    lp_tokens: uint256 = staticcall CRYPTOPOOL.calc_token_amount([debt, assets], True)
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
    frac: uint256 = 10**18 * v.total // (v.total + admin_balance) * tokens // v.supply_tokens
    withdrawn_lp: uint256 = state.collateral * frac // 10**18
    withdrawn_debt: uint256 = state.debt * frac // 10**18
    return staticcall CRYPTOPOOL.calc_withdraw_fixed_out(withdrawn_lp, 0, withdrawn_debt)


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
    staker: address = self.staker
    assert receiver != staker, "Deposit to staker"

    amm: LevAMM = self.amm
    assert extcall STABLECOIN.transferFrom(amm.address, self, debt, default_return_value=True)
    assert extcall ASSET_TOKEN.transferFrom(msg.sender, self, assets, default_return_value=True)
    lp_tokens: uint256 = extcall CRYPTOPOOL.add_liquidity([debt, assets], 0, amm.address, False)
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

    if supply > 0 and liquidity_values.total > 0:
        supply = liquidity_values.supply_tokens
        self.liquidity.admin = liquidity_values.admin
        value_before: uint256 = liquidity_values.total
        value_after = convert(convert(value_after, int256) - liquidity_values.admin, uint256)
        self.liquidity.total = value_after
        self.liquidity.staked = liquidity_values.staked
        self.totalSupply = liquidity_values.supply_tokens  # will be increased by mint
        if staker != empty(address):
            self.balanceOf[staker] = liquidity_values.staked_tokens
            self._log_token_reduction(staker, liquidity_values.token_reduction)
        # ideal_staked is only changed when we transfer coins to staker
        shares = supply * value_after // value_before - supply

    else:
        # Initial value/shares ratio is EXACTLY 1.0 in collateral units
        # Value is measured in USD
        shares = value_after
        # self.liquidity.admin is 0 at start but can be rolled over if everything was withdrawn
        self.liquidity.ideal_staked = 0         # Likely already 0 since supply was 0
        self.liquidity.staked = 0               # Same: nothing staked when supply is 0
        self.liquidity.total = shares + supply  # 1 share = 1 crypto at first deposit
        self.liquidity.admin = 0                # if we had admin fees - give them to the first depositor; simpler to handle
        self.balanceOf[staker] = 0

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
    @param receiver Receiver of the assets who is optional. If not specified - receiver is the sender
    """
    assert shares > 0, "Withdrawing nothing"

    staker: address = self.staker
    assert staker not in [msg.sender, receiver], "Withdraw to/from staker"

    assert not (staticcall self.amm.is_killed()), "We're dead. Use emergency_withdraw"

    amm: LevAMM = self.amm
    liquidity_values: LiquidityValuesOut = self._calculate_values(self._price_oracle_w())
    supply: uint256 = liquidity_values.supply_tokens
    self.liquidity.admin = liquidity_values.admin
    self.liquidity.total = liquidity_values.total
    self.liquidity.staked = liquidity_values.staked
    self.totalSupply = supply

    assert supply >= MIN_SHARE_REMAINDER + shares or supply == shares, "Remainder too small"

    if staker != empty(address):
        self.balanceOf[staker] = liquidity_values.staked_tokens
        self._log_token_reduction(staker, liquidity_values.token_reduction)

    admin_balance: uint256 = convert(max(liquidity_values.admin, 0), uint256)

    withdrawn: Pair = extcall amm._withdraw(10**18 * liquidity_values.total // (liquidity_values.total + admin_balance) * shares // supply)
    assert extcall CRYPTOPOOL.transferFrom(amm.address, self, withdrawn.collateral, default_return_value=True)
    crypto_received: uint256 = extcall CRYPTOPOOL.remove_liquidity_fixed_out(withdrawn.collateral, 0, withdrawn.debt, 0)

    self._burn(msg.sender, shares)  # Changes self.totalSupply
    self.liquidity.total = liquidity_values.total * (supply - shares) // supply
    if liquidity_values.admin < 0:
        # If admin fees are negative - we are skipping them, so reduce proportionally
        self.liquidity.admin = liquidity_values.admin * convert(supply - shares, int256) // convert(supply, int256)
    assert crypto_received >= min_assets, "Slippage"
    assert extcall STABLECOIN.transfer(amm.address, withdrawn.debt, default_return_value=True)
    assert extcall ASSET_TOKEN.transfer(receiver, crypto_received, default_return_value=True)

    log Withdraw(sender=msg.sender, receiver=receiver, owner=msg.sender, assets=crypto_received, shares=shares)
    return crypto_received


@external
@view
def preview_emergency_withdraw(shares: uint256) -> (uint256, int256):
    """
    @notice Method to simulate repay of the debt from the wallet and withdraw what is in the AMM. Does not use heavy math but
            does not necessarily work as single asset withdrawal
    @param shares Shares to withdraw
    @return (unsigned collateral, signed stables). If stables < 0 - we need to bring them
    """
    supply: uint256 = 0
    lv: LiquidityValuesOut = empty(LiquidityValuesOut)
    amm: LevAMM = self.amm

    if staticcall amm.is_killed():
        supply = self.totalSupply
    else:
        lv = self._calculate_values(self._price_oracle())
        supply = lv.supply_tokens

    frac: uint256 = 10**18 * shares // supply
    if lv.admin > 0 and lv.total != 0:
        frac = frac * lv.total // (convert(lv.admin, uint256) + lv.total)

    lp_collateral: uint256 = (staticcall amm.collateral_amount()) * frac // 10**18
    debt: int256 = convert(math._ceil_div((staticcall amm.get_debt()) * frac, 10**18), int256)

    cryptopool_supply: uint256 = staticcall CRYPTOPOOL.totalSupply()
    withdraw_amounts: uint256[2] = [staticcall CRYPTOPOOL.balances(0), staticcall CRYPTOPOOL.balances(1)]
    withdraw_amounts = [
        lp_collateral * withdraw_amounts[0] // cryptopool_supply,
        lp_collateral * withdraw_amounts[1] // cryptopool_supply
    ]

    return (withdraw_amounts[1], convert(withdraw_amounts[0], int256) - debt)


@external
@nonreentrant
def emergency_withdraw(shares: uint256, receiver: address = msg.sender) -> (uint256, int256):
    """
    @notice Method to repay the debt from the wallet and withdraw what is in the AMM. Does not use heavy math but
            does not necessarily work as single asset withdrawal. Minimal output is not specified: convexity of
            bonding curves ensures that attackers can only lose value, not gain
    @param shares Shares to withdraw
    @param receiver Receiver of the assets who is optional. If not specified - receiver is the sender
    @return (unsigned asset, signed stables). If stables < 0 - we need to bring them
    """
    staker: address = self.staker
    assert staker not in [msg.sender, receiver], "Withdraw to/from staker"

    supply: uint256 = 0
    lv: LiquidityValuesOut = empty(LiquidityValuesOut)
    amm: LevAMM = self.amm
    killed: bool = staticcall amm.is_killed()

    if killed or staker == empty(address):
        supply = self.totalSupply
    else:
        lv = self._calculate_values(self._price_oracle_w())
        supply = lv.supply_tokens
        self.liquidity.admin = lv.admin
        self.liquidity.total = lv.total
        self.liquidity.staked = lv.staked
        self.totalSupply = supply
        self.balanceOf[staker] = lv.staked_tokens
        self._log_token_reduction(staker, lv.token_reduction)

    assert supply >= MIN_SHARE_REMAINDER + shares or supply == shares, "Remainder too small"

    frac: uint256 = 10**18 * shares // supply
    frac_clean: int256 = convert(frac, int256)
    if lv.admin > 0 and lv.total != 0:
        frac = frac * lv.total // (convert(lv.admin, uint256) + lv.total)

    withdrawn_levamm: Pair = extcall amm._withdraw(frac)
    assert extcall CRYPTOPOOL.transferFrom(amm.address, self, withdrawn_levamm.collateral, default_return_value=True)
    withdrawn_cswap: uint256[2] = extcall CRYPTOPOOL.remove_liquidity(withdrawn_levamm.collateral, [0, 0])
    stables_to_return: int256 = convert(withdrawn_cswap[0], int256) - convert(withdrawn_levamm.debt, int256)

    if stables_to_return > 0:
        assert extcall STABLECOIN.transfer(receiver, convert(stables_to_return, uint256), default_return_value=True)
    elif stables_to_return < 0:
        assert extcall STABLECOIN.transferFrom(msg.sender, self, convert(-stables_to_return, uint256), default_return_value=True)
    assert extcall STABLECOIN.transfer(amm.address, withdrawn_levamm.debt, default_return_value=True)
    assert extcall ASSET_TOKEN.transfer(receiver, withdrawn_cswap[1], default_return_value=True)

    self._burn(msg.sender, shares)

    self.liquidity.total = self.liquidity.total * (supply - shares) // supply
    if self.liquidity.admin < 0 or killed:
        self.liquidity.admin = self.liquidity.admin * (10**18 - frac_clean) // 10**18

    return (withdrawn_cswap[1], stables_to_return)


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
    self._check_admin()
    assert self.amm == empty(LevAMM), "Already set"
    assert staticcall amm.STABLECOIN() == STABLECOIN.address
    assert staticcall amm.COLLATERAL() == CRYPTOPOOL.address
    assert staticcall amm.LT_CONTRACT() == self
    self.amm = amm
    self.agg = PriceOracle(staticcall (staticcall amm.PRICE_ORACLE_CONTRACT()).AGG())


@external
@nonreentrant
def set_admin(new_admin: address):
    self._check_admin()
    self.admin = new_admin
    log SetAdmin(admin=new_admin)


@external
@nonreentrant
def set_rate(rate: uint256):
    self._check_admin()
    extcall self.amm.set_rate(rate)


@external
@nonreentrant
def set_amm_fee(fee: uint256):
    self._check_admin()
    extcall self.amm.set_fee(fee)


@external
@nonreentrant
def allocate_stablecoins(limit: uint256 = max_value(uint256)):
    """
    @notice This method has to be used once this contract has received allocation of stablecoins
    @param limit Limit to allocate for this pool from this allocator. Max uint256 = do not change
    """
    allocator: address = self.admin
    allocation: uint256 = limit
    allocated: uint256 = self.stablecoin_allocated
    if limit == max_value(uint256):
        allocation = self.stablecoin_allocation
    else:
        self._check_admin()
        self.stablecoin_allocation = limit

    extcall self.amm.check_nonreentrant()

    if allocation > allocated:
        # Assume that allocator has everything
        assert extcall STABLECOIN.transferFrom(allocator, self.amm.address, allocation - allocated, default_return_value=True)
        self.stablecoin_allocated = allocation

    elif allocation < allocated:
        lp_price: uint256 = extcall (staticcall self.amm.PRICE_ORACLE_CONTRACT()).price_w()
        assert allocation >= lp_price * (staticcall self.amm.collateral_amount()) // 10**18, "Not enough stables"
        to_transfer: uint256 = min(allocated - allocation, staticcall STABLECOIN.balanceOf(self.amm.address))
        allocated -= to_transfer
        assert extcall STABLECOIN.transferFrom(self.amm.address, allocator, to_transfer, default_return_value=True)
        self.stablecoin_allocated = allocated

    log AllocateStablecoins(allocator=allocator, stablecoin_allocation=allocation, stablecoin_allocated=allocated)


@external
@nonreentrant
def distribute_borrower_fees(discount: uint256 = FEE_CLAIM_DISCOUNT):  # This will JUST donate to the crypto pool
    if discount > FEE_CLAIM_DISCOUNT:
        self._check_admin()
    extcall self.amm.collect_fees()
    amount: uint256 = staticcall STABLECOIN.balanceOf(self)
    # We price to the stablecoin we use, not the aggregated USD here, and this is correct
    min_amount: uint256 = (10**18 - discount) * amount // staticcall CRYPTOPOOL.lp_price()
    extcall CRYPTOPOOL.add_liquidity([amount, 0], min_amount, self, True)
    log DistributeBorrowerFees(sender=msg.sender, amount=amount, min_amount=min_amount, discount=discount)


@external
@nonreentrant
def withdraw_admin_fees():
    admin: address = self.admin
    assert admin.is_contract, "Need factory"
    assert msg.sender == staticcall Factory(admin).admin(), "Access"

    assert not staticcall self.amm.is_killed(), "Killed"

    fee_receiver: address = staticcall Factory(admin).fee_receiver()
    assert fee_receiver != empty(address), "No fee_receiver"

    staker: address = self.staker
    assert fee_receiver != staker, "Staker=fee_receiver"

    v: LiquidityValuesOut = self._calculate_values(self._price_oracle_w())
    assert v.admin >= 0, "Loss made admin fee negative"
    self.totalSupply = v.supply_tokens
    # Mint YB tokens to fee receiver and burn the untokenized admin buffer at the same time
    # fee_receiver is just a normal user
    new_total: uint256 = v.total + convert(v.admin, uint256)
    to_mint: uint256 = v.supply_tokens * new_total // v.total - v.supply_tokens
    self._mint(fee_receiver, to_mint)
    self.liquidity.total = new_total
    self.liquidity.admin = 0
    self.liquidity.staked = v.staked
    if staker != empty(address):
        self.balanceOf[staker] = v.staked_tokens
        self._log_token_reduction(staker, v.token_reduction)

    log WithdrawAdminFees(receiver=fee_receiver, amount=to_mint)


@external
@nonreentrant
def set_staker(staker: address):
    assert self.staker == empty(address), "Staker already set"
    assert staker != empty(address)
    self._check_admin()

    staker_balance: uint256 = self.balanceOf[staker]
    if staker_balance > 0:
        # Take that all as admin fee, staker should not have this
        fee_receiver: address = staticcall Factory(self.admin).fee_receiver()
        self._transfer(staker, fee_receiver, staker_balance)

    self.staker = staker
    log SetStaker(staker=staker)


@external
def set_killed(is_killed: bool):
    admin: address = self.admin
    if admin.is_contract:
        assert msg.sender in [admin, staticcall Factory(admin).admin(), staticcall Factory(admin).emergency_admin()], "Access"
    else:
        assert msg.sender == admin, "Access"
    extcall self.amm.set_killed(is_killed)


# ERC20 methods

@external
@view
def symbol() -> String[32]:
    return concat('yb-', staticcall IERC20Slice(ASSET_TOKEN.address).symbol())


@external
@view
def name() -> String[58]:
    return concat('Yield Basis liquidity for ', staticcall IERC20Slice(ASSET_TOKEN.address).symbol())


@internal
def _approve(_owner: address, _spender: address, _value: uint256):
    self.allowance[_owner][_spender] = _value

    log IERC20.Approval(owner=_owner, spender=_spender, value=_value)


@internal
def _burn(_from: address, _value: uint256):
    self.balanceOf[_from] -= _value
    self.totalSupply -= _value

    log IERC20.Transfer(sender=_from, receiver=empty(address), value=_value)


@internal
def _mint(_to: address, _value: uint256):
    self.balanceOf[_to] += _value
    self.totalSupply += _value

    log IERC20.Transfer(sender=empty(address), receiver=_to, value=_value)


@internal
def _transfer(_from: address, _to: address, _value: uint256):
    assert _to not in [self, empty(address)]

    killed: bool = staticcall self.amm.is_killed()

    staker: address = self.staker
    if staker != empty(address) and staker in [_from, _to]:
        assert _from != _to
        liquidity: LiquidityValuesOut = empty(LiquidityValuesOut)

        if killed:
            liquidity.ideal_staked = self.liquidity.ideal_staked
            liquidity.staked = self.liquidity.staked
            liquidity.total = self.liquidity.total
            liquidity.supply_tokens = self.totalSupply
            liquidity.staked_tokens = self.balanceOf[staker]
        else:
            liquidity = self._calculate_values(self._price_oracle_w())
            self.liquidity.admin = liquidity.admin
            self.liquidity.total = liquidity.total
            self.totalSupply = liquidity.supply_tokens
            self.balanceOf[staker] = liquidity.staked_tokens
            self._log_token_reduction(staker, liquidity.token_reduction)

        if _from == staker:
            # Reduce the staked part
            # change by 0 if no supply_tokens or stake_tokens found
            liquidity.staked -= unsafe_div(liquidity.total * _value, liquidity.supply_tokens)
            liquidity.ideal_staked = unsafe_div(liquidity.ideal_staked * (liquidity.staked_tokens - _value), liquidity.staked_tokens)
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

    log IERC20.Transfer(sender=_from, receiver=_to, value=_value)


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
