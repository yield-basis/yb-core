# pragma version 0.4.3
"""
@title Twocrypto
@author Curve.Fi
@license Copyright (c) Curve.Fi, 2025 - all rights reserved
@notice A Curve AMM pool for 2 unpegged assets (e.g. WETH, USD).
@dev All prices in the AMM are with respect to the first token in the pool.
"""

from ethereum.ercs import IERC20
implements: IERC20  # <--------------------- AMM contract is also the LP token.

# --------------------------------- Interfaces -------------------------------

interface Math:
    def wad_exp(_power: int256) -> uint256: view
    def newton_D(
        ANN: uint256,
        gamma: uint256,
        x_unsorted: uint256[N_COINS],
        K0_prev: uint256
    ) -> uint256: view
    def get_y(
        ANN: uint256,
        gamma: uint256,
        x: uint256[N_COINS],
        D: uint256,
        i: uint256,
    ) -> uint256[2]: view
    def get_p(
        _xp: uint256[N_COINS],
        _D: uint256,
        _A_gamma: uint256[2],
    ) -> uint256: view

interface Factory:
    def admin() -> address: view
    def fee_receiver() -> address: view

interface Views:
    def calc_token_amount(
        amounts: uint256[N_COINS], deposit: bool, swap: address
    ) -> uint256: view
    def get_dy(
        i: uint256, j: uint256, dx: uint256, swap: address
    ) -> uint256: view
    def get_dx(
        i: uint256, j: uint256, dy: uint256, swap: address
    ) -> uint256: view


# ------------------------------- Events -------------------------------------

event SetViews:
    views: Views

event Transfer:
    sender: indexed(address)
    receiver: indexed(address)
    value: uint256

event Approval:
    owner: indexed(address)
    spender: indexed(address)
    value: uint256

event TokenExchange:
    buyer: indexed(address)
    sold_id: uint256
    tokens_sold: uint256
    bought_id: uint256
    tokens_bought: uint256
    fee: uint256
    price_scale: uint256

event AddLiquidity:
    receiver: indexed(address)
    token_amounts: uint256[N_COINS]
    fee: uint256
    token_supply: uint256
    price_scale: uint256

event Donation:
    donor: indexed(address)
    token_amounts: uint256[N_COINS]

event RemoveLiquidity:
    provider: indexed(address)
    token_amounts: uint256[N_COINS]
    token_supply: uint256

event RemoveLiquidityImbalance:
    provider: indexed(address)
    lp_token_amount: uint256
    token_amounts: uint256[N_COINS]
    approx_fee: uint256
    price_scale: uint256

event NewParameters:
    mid_fee: uint256
    out_fee: uint256
    fee_gamma: uint256
    allowed_extra_profit: uint256
    adjustment_step: uint256
    ma_time: uint256

event RampAgamma:
    initial_A: uint256
    future_A: uint256
    initial_gamma: uint256
    future_gamma: uint256
    initial_time: uint256
    future_time: uint256

event StopRampA:
    current_A: uint256
    current_gamma: uint256
    time: uint256

event ClaimAdminFee:
    admin: indexed(address)
    tokens: uint256[N_COINS]

event SetDonationDuration:
    duration: uint256

event SetDonationProtection:
    donation_protection_period: uint256
    donation_protection_lp_threshold: uint256

event SetAdminFee:
    admin_fee: uint256

# ----------------------- Storage/State Variables ----------------------------

N_COINS: constant(uint256) = 2
PRECISION: constant(uint256) = 10**18  # <------- The precision to convert to.
PRECISIONS: immutable(uint256[N_COINS])

MATH: public(immutable(Math))
coins: public(immutable(address[N_COINS]))
factory: public(immutable(Factory))

view_contract: public(Views)

cached_price_scale: uint256  # <------------------------ Internal price scale.
cached_price_oracle: uint256  # <------- Price target given by moving average.

last_prices: public(uint256)
last_timestamp: public(uint256)

initial_A_gamma: public(uint256)
initial_A_gamma_time: public(uint256)

future_A_gamma: public(uint256)
future_A_gamma_time: public(uint256)  # <------ Time when ramping is finished.
#         This value is 0 (default) when pool is first deployed, and only gets
#        populated by block.timestamp + future_time in `ramp_A_gamma` when the
#                      ramping process is initiated. After ramping is finished
#      (i.e. self.future_A_gamma_time < block.timestamp), the variable is left
#                                                            and not set to 0.

# Donation shares balance
donation_shares: public(uint256)
# Donations release parameters:
donation_duration: public(uint256)
last_donation_release_ts: public(uint256)

# Donation protection
donation_protection_expiry_ts: public(uint256)
donation_protection_period: public(uint256)
donation_protection_lp_threshold: public(uint256)

balances: public(uint256[N_COINS])
D: public(uint256)
xcp_profit: public(uint256)
xcp_profit_a: public(uint256)  # <--- Full profit at last claim of admin fees.

virtual_price: public(uint256)  # <------ Cached (fast to read) virtual price.
#                          The cached `virtual_price` is also used internally.

# Params that affect how price_scale get adjusted :
packed_rebalancing_params: public(uint256)  # <---------- Contains rebalancing
#               parameters allowed_extra_profit, adjustment_step, and ma_time.

# Fee params that determine dynamic fees:
packed_fee_params: public(uint256)  # <---- Packs mid_fee, out_fee, fee_gamma.

admin_fee: public(uint256)
MAX_ADMIN_FEE: constant(uint256) = 10**10
MIN_FEE: constant(uint256) = 5 * 10**5  # <-------------------------- 0.5 BPS.
MAX_FEE: constant(uint256) = 10 * 10**9
NOISE_FEE: constant(uint256) = 10**5  # <---------------------------- 0.1 BPS.

# ----------------------- Admin params ---------------------------------------

last_admin_fee_claim_timestamp: uint256

MIN_RAMP_TIME: constant(uint256) = 86400
MIN_ADMIN_FEE_CLAIM_INTERVAL: constant(uint256) = 86400

A_MULTIPLIER: constant(uint256) = 10000
# Note on pool internal logic:
# A is scaled by N_COINS in context of StableswapMath.vy
# So A := A_true * N_COINS
MIN_A: constant(uint256) = N_COINS**(N_COINS-1) * A_MULTIPLIER // 10
MAX_A: constant(uint256) = N_COINS**(N_COINS-0) * A_MULTIPLIER * 1000 #relax max constraint
MAX_PARAM_CHANGE: constant(uint256) = 10
MIN_GAMMA: constant(uint256) = 10**10
MAX_GAMMA: constant(uint256) = 199 * 10**15 # 1.99 * 10**17

# ----------------------- ERC20 Specific vars --------------------------------

name: public(immutable(String[64]))
symbol: public(immutable(String[32]))
decimals: public(constant(uint8)) = 18
version: public(constant(String[8])) = "v2.1.0"

balanceOf: public(HashMap[address, uint256])
allowance: public(HashMap[address, HashMap[address, uint256]])
totalSupply: public(uint256)

# ----------------------- Contract -------------------------------------------

@deploy
def __init__(
    _name: String[64],
    _symbol: String[32],
    _coins: address[N_COINS],
    _math: address,
    _salt: bytes32, # not used, left for compatibility with legacy factory
    packed_precisions: uint256,
    packed_gamma_A: uint256,
    packed_fee_params: uint256,
    packed_rebalancing_params: uint256,
    initial_price: uint256,
):

    MATH = Math(_math)

    factory = Factory(msg.sender)
    name = _name
    symbol = _symbol
    coins = _coins

    PRECISIONS = self._unpack_2(packed_precisions)  # <-- Precisions of coins.

    # --------------- Validate A and gamma parameters here and not in factory.
    gamma_A: uint256[2] = self._unpack_2(packed_gamma_A)  # gamma is at idx 0.

    assert gamma_A[0] > MIN_GAMMA-1, "gamma<MIN"
    assert gamma_A[0] < MAX_GAMMA+1, "gamma>MAX"

    assert gamma_A[1] > MIN_A-1, "A<MIN"
    assert gamma_A[1] < MAX_A+1, "A>MAX"

    self.initial_A_gamma = packed_gamma_A
    self.future_A_gamma = packed_gamma_A
    # ------------------------------------------------------------------------

    self.packed_rebalancing_params = packed_rebalancing_params  # <-- Contains
    #               rebalancing params: allowed_extra_profit, adjustment_step,
    #                                                         and ma_exp_time.

    self.packed_fee_params = packed_fee_params  # <-------------- Contains Fee
    #                                  params: mid_fee, out_fee and fee_gamma.

    self.cached_price_scale = initial_price
    self.cached_price_oracle = initial_price
    self.last_prices = initial_price
    self.last_timestamp = block.timestamp
    self.xcp_profit_a = 10**18

    self.donation_duration = 7 * 86400

    self.admin_fee = 5 * 10**9

    self.donation_protection_expiry_ts = 0
    self.donation_protection_period = 10 * 60   # 10 minutes
    self.donation_protection_lp_threshold = 20 * PRECISION // 100  # 20%

    log Transfer(sender=empty(address), receiver=self, value=0)  # <------- Fire empty transfer from
    #                                       0x0 to self for indexers to catch.


# ------------------- Token transfers in and out of the AMM ------------------


@internal
def _transfer_in(
    _coin_idx: uint256,
    _dx: uint256,
    sender: address,
    expect_optimistic_transfer: bool,
) -> uint256:
    """
    @notice Transfers `_coin` from `sender` to `self` and calls `callback_sig`
            if it is not empty.
    @params _coin_idx uint256 Index of the coin to transfer in.
    @params dx amount of `_coin` to transfer into the pool.
    @params sender address to transfer `_coin` from.
    @params expect_optimistic_transfer bool True if pool expects user to transfer.
            This is only enabled for exchange_received.
    @return The amount of tokens received.
    """
    coin_balance: uint256 = staticcall IERC20(coins[_coin_idx]).balanceOf(self)

    if expect_optimistic_transfer:  # Only enabled in exchange_received:
        # it expects the caller of exchange_received to have sent tokens to
        # the pool before calling this method.

        # If someone donates extra tokens to the contract: do not acknowledge.
        # We only want to know if there are dx amount of tokens. Anything extra,
        # we ignore. This is why we need to check if received_amounts (which
        # accounts for coin balances of the contract) is atleast dx.
        # If we checked for received_amounts == dx, an extra transfer without a
        # call to exchange_received will break the method.
        dx: uint256 = coin_balance - self.balances[_coin_idx]
        assert dx >= _dx, "user didn't give us coins"

        # Adjust balances
        self.balances[_coin_idx] += dx

        return dx

    # ----------------------------------------------- ERC20 transferFrom flow.

    # EXTERNAL CALL
    assert extcall IERC20(coins[_coin_idx]).transferFrom(
        sender,
        self,
        _dx,
        default_return_value=True
    ), "transferFrom failed"

    dx: uint256 = staticcall IERC20(coins[_coin_idx]).balanceOf(self) - coin_balance
    self.balances[_coin_idx] += dx
    return dx


@internal
def _transfer_out(_coin_idx: uint256, _amount: uint256, receiver: address):
    """
    @notice Transfer a single token from the pool to receiver.
    @params _coin_idx uint256 Index of the token to transfer out
    @params _amount Amount of token to transfer out
    @params receiver Address to send the tokens to
    """

    # Adjust balances before handling transfers:
    self.balances[_coin_idx] -= _amount

    # EXTERNAL CALL
    assert extcall IERC20(coins[_coin_idx]).transfer(
        receiver,
        _amount,
        default_return_value=True
    ), "transfer failed"


# -------------------------- AMM Main Functions ------------------------------


@external
@nonreentrant
def exchange(
    i: uint256,
    j: uint256,
    dx: uint256,
    min_dy: uint256,
    receiver: address = msg.sender
) -> uint256:
    """
    @notice Exchange using wrapped native token by default
    @param i Index value for the input coin
    @param j Index value for the output coin
    @param dx Amount of input coin being swapped in
    @param min_dy Minimum amount of output coin to receive
    @param receiver Address to send the output coin to. Default is msg.sender
    @return uint256 Amount of tokens at index j received by the `receiver
    """
    # _transfer_in updates self.balances here:
    dx_received: uint256 = self._transfer_in(
        i,
        dx,
        msg.sender,
        False
    )

    # No ERC20 token transfers occur here:
    out: uint256[3] = self._exchange(
        i,
        j,
        dx_received,
        min_dy,
    )

    # _transfer_out updates self.balances here. Update to state occurs before
    # external calls:
    self._transfer_out(j, out[0], receiver)

    # log:
    log TokenExchange(buyer=msg.sender, sold_id=i, tokens_sold=dx_received, bought_id=j, tokens_bought=out[0], fee=out[1], price_scale=out[2])

    return out[0]


@external
@nonreentrant
def exchange_received(
    i: uint256,
    j: uint256,
    dx: uint256,
    min_dy: uint256,
    receiver: address = msg.sender,
) -> uint256:
    """
    @notice Exchange: but user must transfer dx amount of coin[i] tokens to pool first.
            Pool will not call transferFrom and will only check if a surplus of
            coins[i] is greater than or equal to `dx`.
    @dev Use-case is to reduce the number of redundant ERC20 token
         transfers in zaps. Primarily for dex-aggregators/arbitrageurs/searchers.
         Note for users: please transfer + exchange_received in 1 tx.
    @param i Index value for the input coin
    @param j Index value for the output coin
    @param dx Amount of input coin being swapped in
    @param min_dy Minimum amount of output coin to receive
    @param receiver Address to send the output coin to
    @return uint256 Amount of tokens at index j received by the `receiver`
    """
    # _transfer_in updates self.balances here:
    dx_received: uint256 = self._transfer_in(
        i,
        dx,
        msg.sender,
        True  # <---- expect_optimistic_transfer is set to True here.
    )

    # No ERC20 token transfers occur here:
    out: uint256[3] = self._exchange(
        i,
        j,
        dx_received,
        min_dy,
    )

    # _transfer_out updates self.balances here. Update to state occurs before
    # external calls:
    self._transfer_out(j, out[0], receiver)

    # log:
    log TokenExchange(buyer=msg.sender, sold_id=i, tokens_sold=dx_received, bought_id=j, tokens_bought=out[0], fee=out[1], price_scale=out[2])

    return out[0]


@internal
@view
def _donation_shares(_donation_protection: bool = True) -> uint256:
    """
    @notice Calculates the amount of donation shares that are unlocked and not under protection.
    @dev This function accounts for both time-based release and add_liquidity-based protection.
    """
    donation_shares: uint256 = self.donation_shares
    if donation_shares == 0:
        return 0

    # --- Time-based release of donation shares ---
    elapsed: uint256 = block.timestamp - self.last_donation_release_ts
    unlocked_shares: uint256 = min(donation_shares, donation_shares * elapsed // self.donation_duration)

    if not _donation_protection:
        # if donation protection is disabled, return the total amount of unlocked donation shares
        # this is needed to calculate new timestamp for overlapping donations in add_liquidity
        # otherwise must always be called with donation_protection=True
        return unlocked_shares

    # --- Donation protection damping factor ---
    protection_factor: uint256 = 0
    expiry: uint256 = self.donation_protection_expiry_ts
    if expiry > block.timestamp:
        protection_factor = min((expiry - block.timestamp) * PRECISION // self.donation_protection_period, PRECISION)

    return unlocked_shares * (PRECISION - protection_factor) // PRECISION


@external
@nonreentrant
def add_liquidity(
    amounts: uint256[N_COINS],
    min_mint_amount: uint256,
    receiver: address = msg.sender,
    donation: bool = False
) -> uint256:
    """
    @notice Adds liquidity into the pool.
    @param amounts Amounts of each coin to add.
    @param min_mint_amount Minimum amount of LP to mint.
    @param receiver Address to send the LP tokens to. Default is msg.sender
    @param donation Whether the liquidity is a donation, if True receiver is ignored.
    @return uint256 Amount of LP tokens issued (to receiver or donation buffer).
    """


    assert amounts[0] + amounts[1] > 0, "no coins to add"

    # --------------------- Get prices, balances -----------------------------

    old_balances: uint256[N_COINS] = self.balances

    ########################## TRANSFER IN <-------

    amounts_received: uint256[N_COINS] = empty(uint256[N_COINS])
    # This variable will contain the old balances + the amounts received.
    balances: uint256[N_COINS] = self.balances
    for i: uint256 in range(N_COINS):
        if amounts[i] > 0:
            # Updates self.balances here:
            amounts_received[i] = self._transfer_in(
                i,
                amounts[i],
                msg.sender,
                False,  # <--------------------- Disable optimistic transfers.
            )
            balances[i] += amounts_received[i]

    price_scale: uint256 = self.cached_price_scale
    xp: uint256[N_COINS] = self._xp(balances, price_scale)
    old_xp: uint256[N_COINS] = self._xp(old_balances, price_scale)

    # -------------------- Calculate LP tokens to mint -----------------------

    A_gamma: uint256[2] = self._A_gamma()
    old_D: uint256 = self._get_D(A_gamma, old_xp)

    D: uint256 = staticcall MATH.newton_D(A_gamma[0], A_gamma[1], xp, 0)

    token_supply: uint256 = self.totalSupply
    d_token: uint256 = 0
    if old_D > 0:
        d_token = token_supply * D // old_D - token_supply
    else:
        d_token = self._xcp(D, price_scale)  # <----- Making initial virtual price equal to 1.

    assert d_token > 0, "nothing minted"


    d_token_fee: uint256 = 0
    if old_D > 0:
        d_token_fee = (
            self._calc_token_fee(amounts_received, xp, donation, True) * d_token // 10**10 + 1
        ) # for donations - we only take NOISE_FEE (check _calc_token_fee)
        d_token -= d_token_fee

        if donation:
            assert receiver == empty(address), "nonzero receiver"
            new_donation_shares: uint256 = self.donation_shares + d_token

            # When adding donation, if the previous one hasn't been fully released we preserve
            # the currently unlocked donation [given by `self._donation_shares()`] by updating
            # `self.last_donation_release_ts` as if a single virtual donation of size `new_donation_shares`
            # was made in past and linearly unlocked reaching `self._donation_shares()` at the current time.

            # We want the following equality to hold:
            # self._donation_shares() = new_donation_shares * (new_elapsed / self.donation_duration)
            # We can rearrange this to find the new elapsed time (imitating one large virtual donation):
            # => new_elapsed = self._donation_shares() * self.donation_duration / new_donation_shares
            # edge case: if self.donation_shares = 0, then self._donation_shares() is 0
            # and new_elapsed = 0, thus initializing last_donation_release_ts = block.timestamp
            new_elapsed: uint256 = self._donation_shares(False) * self.donation_duration // new_donation_shares

            # Additional observations:
            # new_elapsed = (old_pool * old_elapsed / D) * D / new_pool = old_elapsed * (old_pool / new_pool)
            # => new_elapsed is always smaller than old_elapsed
            # and self.last_donation_release_ts is carried forward propotionally to new donation size.
            self.last_donation_release_ts = block.timestamp - new_elapsed

            # Credit donation: we don't explicitly mint lp tokens, but increase total supply
            self.donation_shares = new_donation_shares
            self.totalSupply += d_token
            log Donation(donor=msg.sender, token_amounts=amounts_received)
        else:
            # --- Donation Protection & LP Spam Penalty ---
            # Extend protection to shield against donation extraction via sandwich attacks.
            # A penalty is applied for extending the protection to disincentivize spamming.
            relative_lp_add: uint256 = d_token * PRECISION // (token_supply + d_token)
            if relative_lp_add > 0 and self.donation_shares > 0:  # sub-precision additions are expensive to stack
                # Extend protection period
                protection_period: uint256 = self.donation_protection_period
                extension_seconds: uint256 = min(relative_lp_add * protection_period // self.donation_protection_lp_threshold, protection_period)
                current_expiry: uint256 = max(self.donation_protection_expiry_ts, block.timestamp)
                new_expiry: uint256 = min(current_expiry + extension_seconds, block.timestamp + protection_period)
                self.donation_protection_expiry_ts = new_expiry

            # Regular liquidity addition
            self.mint(receiver, d_token)

        price_scale = self.tweak_price(A_gamma, xp, D)

    else:

        # (re)instatiating an empty pool:

        self.D = D
        self.virtual_price = 10**18
        self.xcp_profit = 10**18
        self.xcp_profit_a = 10**18

        self.mint(receiver, d_token)
    assert d_token >= min_mint_amount, "slippage"

    # ---------------------------------------------- Log and claim admin fees.

    log AddLiquidity(
        receiver=receiver,
        token_amounts=amounts_received,
        fee=d_token_fee,
        token_supply=token_supply+d_token,
        price_scale=price_scale
    )

    return d_token


@external
@nonreentrant
def remove_liquidity(
    amount: uint256,
    min_amounts: uint256[N_COINS],
    receiver: address = msg.sender,
) -> uint256[N_COINS]:
    """
    @notice This withdrawal method is very safe, does no complex math since
            tokens are withdrawn in balanced proportions. No fees are charged.
    @dev This function intentionally does not rely on any external call to the
            the math contract to make sure that failures in the invariant don't
            prevent users from withdrawing their funds.
    @param amount Amount of LP tokens to burn
    @param min_amounts Minimum amounts of tokens to withdraw
    @param receiver Address to send the withdrawn tokens to
    @return uint256[N_COINS] Amount of pool tokens received by the `receiver`
    """


    # -------------------------------------------------------- Burn LP tokens.

    # We cache the total supply to avoid multiple SLOADs. It is important to do
    # this before the burnFrom call, as the burnFrom call will reduce the supply.
    total_supply: uint256 = self.totalSupply
    self.burnFrom(msg.sender, amount)

    # There are two cases for withdrawing tokens from the pool.
    #   Case 1. Withdrawal does not empty the pool.
    #           In this situation, D is adjusted proportional to the amount of
    #           LP tokens burnt. ERC20 tokens transferred is proportional
    #           to : (AMM balance * LP tokens in) / LP token total supply
    #   Case 2. Withdrawal empties the pool.
    #           In this situation, all tokens are withdrawn and the invariant
    #           is reset.

    withdraw_amounts: uint256[N_COINS] = empty(uint256[N_COINS])
    D: uint256 = self.D # no ramping adjustment to preserve safety of balanced removal

    if amount == total_supply:  # <----------------------------------- Case 2.

        for i: uint256 in range(N_COINS):

            withdraw_amounts[i] = self.balances[i]

    else:  # <-------------------------------------------------------- Case 1.
        for i: uint256 in range(N_COINS):
            # TODO improve comments here
            # Withdraws slightly less -> favors LPs already
            withdraw_amounts[i] = self.balances[i] * amount // total_supply

            assert withdraw_amounts[i] >= min_amounts[i], "slippage"

    # Reduce D proportionally to the amount of tokens leaving. Since withdrawals
    # are balanced, this is a simple subtraction. If amount == total_supply,
    # D will be 0.
    self.D = D - unsafe_div(D * amount, total_supply)

    # ---------------------------------- Transfers ---------------------------

    for i: uint256 in range(N_COINS):
        # _transfer_out updates self.balances here. Update to state occurs
        # before external calls:
        self._transfer_out(i, withdraw_amounts[i], receiver)

    # We intentionally use the unadjusted `amount` here as the amount of lp
    # tokens burnt is `amount`, regardless of the rounding error.
    log RemoveLiquidity(provider=msg.sender, token_amounts=withdraw_amounts, token_supply=total_supply - amount)

    # Take care of leftover donations (only if all LP left)
    self._withdraw_leftover_donations()

    return withdraw_amounts


@external
@nonreentrant
def remove_liquidity_fixed_out(
    token_amount: uint256,
    i: uint256,
    amount_i: uint256,
    min_amount_j: uint256,
    receiver: address = msg.sender
) -> uint256:
    """
    @notice Withdrawal where amount of token i is specified
    @param token_amount LP Token amount to burn
    @param i Index of the coin to withdraw
    @param amount_i exact amount of token i which will be withdrawn
    @param min_amount_j Minimum amount of token j=1-i to withdraw.
    @param receiver Address to send the withdrawn tokens to
    @return Amount of tokens at index j=1-i received by the `receiver`
    """
    return self._remove_liquidity_fixed_out(
        token_amount,
        i,
        amount_i,
        min_amount_j,
        receiver,
    )


@external
@nonreentrant
def remove_liquidity_one_coin(
    lp_token_amount: uint256,
    i: uint256,
    min_amount: uint256,
    receiver: address = msg.sender
) -> uint256:
    """
    @notice Withdraw liquidity in a single coin.
    @param lp_token_amount Amount of LP tokens to burn.
    @param i Index of the coin to withdraw.
    @param min_amount Minimum amount of coin[i] to withdraw.
    @param receiver Address to send the withdrawn tokens to
    @return Amount of coin[i] tokens received by the `receiver`
    """
    return self._remove_liquidity_fixed_out(
        lp_token_amount,
        1 - i, # Here we flip i because we want to constrain the other coin to be zero.
        0, # We set the amount of coin[1 - i] to be withdrawn to 0.
        min_amount,
        receiver,
    )


@internal
def _remove_liquidity_fixed_out(
    token_amount: uint256,
    i: uint256,
    amount_i: uint256,
    min_amount_j: uint256,
    receiver: address,
) -> uint256:

    self._claim_admin_fees()

    A_gamma: uint256[2] = self._A_gamma()

    # Amount of coin[j] withdrawn.
    dy: uint256 = 0
    # New value of D after the withdrawal.
    D: uint256 = 0
    # New scaled balances after the withdrawal.
    xp: uint256[N_COINS] = empty(uint256[N_COINS])
    approx_fee: uint256 = 0

    # ------------------------------------------------------------------------

    dy, D, xp, approx_fee = self._calc_withdraw_fixed_out(
        A_gamma,
        token_amount,
        i,
        amount_i,
    )

    assert dy >= min_amount_j, "slippage"

    # ---------------------------- State Updates -----------------------------

    self.burnFrom(msg.sender, token_amount)

    price_scale: uint256 = self.tweak_price(A_gamma, xp, D)

    self._transfer_out(i, amount_i, receiver)
    self._transfer_out(1 - i, dy, receiver)

    token_amounts: uint256[N_COINS] = empty(uint256[N_COINS])
    token_amounts[i] = amount_i
    token_amounts[1-i] = dy

    log RemoveLiquidityImbalance(
        provider=msg.sender,
        lp_token_amount=token_amount,
        token_amounts=token_amounts,
        approx_fee=approx_fee * token_amount // 10**10 + 1,
        price_scale=price_scale
    )

    # Take care of leftover donations (only if all LP left)
    self._withdraw_leftover_donations()

    return dy


@internal
def _withdraw_leftover_donations():
    """
    @notice Withdraws leftover donations from the pool.
    This is called when the pool has no other liquidity than donation shares,
    and must be emptied.
    @dev donations go to the factory fees receiver, if not set, to the admin.
    """

    if self.donation_shares != self.totalSupply:
        return

    # Pool has no other LP than donation shares, must be emptied
    receiver: address = staticcall factory.fee_receiver()
    if receiver == empty(address):
        receiver = staticcall factory.admin()

    # empty the pool
    withdraw_amounts: uint256[N_COINS] = self.balances

    for i: uint256 in range(N_COINS):
        # updates self.balances here
        self._transfer_out(i, withdraw_amounts[i], receiver)

    # Update state
    self.donation_shares = 0
    self.totalSupply = 0
    self.D = 0
    self.donation_protection_expiry_ts = 0
    log RemoveLiquidity(provider=receiver, token_amounts=withdraw_amounts, token_supply=0)


# -------------------------- Packing functions -------------------------------


@internal
@pure
def _pack_3(x: uint256[3]) -> uint256:
    """
    @notice Packs 3 integers with values <= 10**18 into a uint256
    @param x The uint256[3] to pack
    @return uint256 Integer with packed values
    """
    return (x[0] << 128) | (x[1] << 64) | x[2]


@internal
@pure
def _unpack_3(_packed: uint256) -> uint256[3]:
    """
    @notice Unpacks a uint256 into 3 integers (values must be <= 10**18)
    @param val The uint256 to unpack
    @return uint256[3] A list of length 3 with unpacked integers
    """
    return [
        (_packed >> 128) & 18446744073709551615,
        (_packed >> 64) & 18446744073709551615,
        _packed & 18446744073709551615,
    ]


@pure
@internal
def _pack_2(p1: uint256, p2: uint256) -> uint256:
    return p1 | (p2 << 128)


@pure
@internal
def _unpack_2(packed: uint256) -> uint256[2]:
    return [packed & (2**128 - 1), packed >> 128]



@internal
def _exchange(
    i: uint256,
    j: uint256,
    dx_received: uint256,
    min_dy: uint256,
) -> uint256[3]:

    assert i != j, "same coin"
    assert dx_received > 0, "zero dx"

    A_gamma: uint256[2] = self._A_gamma()
    balances: uint256[N_COINS] = self.balances
    dy: uint256 = 0

    y: uint256 = balances[j]
    x0: uint256 = balances[i] - dx_received  # old xp[i]

    price_scale: uint256 = self.cached_price_scale
    xp: uint256[N_COINS] = self._xp(balances, price_scale)

    # ----------- Update invariant if A, gamma are undergoing ramps ---------

    if self._is_ramping():

        x0 *= PRECISIONS[i]

        if i > 0:
            x0 = unsafe_div(x0 * price_scale, PRECISION)

        x1: uint256 = xp[i]  # <------------------ Back up old value in xp ...
        xp[i] = x0                                                         # |
        self.D = staticcall MATH.newton_D(A_gamma[0], A_gamma[1], xp, 0)   # |
        xp[i] = x1  # <-------------------------------------- ... and restore.

    # ----------------------- Calculate dy and fees --------------------------

    D: uint256 = self.D
    y_out: uint256[2] = staticcall MATH.get_y(A_gamma[0], A_gamma[1], xp, D, j)
    dy = xp[j] - y_out[0]
    xp[j] -= dy
    dy -= 1

    if j > 0:
        dy = dy * PRECISION // price_scale
    dy //= PRECISIONS[j]

    fee: uint256 = unsafe_div(self._fee(xp) * dy, 10**10)
    dy -= fee  # <--------------------- Subtract fee from the outgoing amount.
    assert dy >= min_dy, "slippage"
    y -= dy

    y *= PRECISIONS[j]
    if j > 0:
        y = unsafe_div(y * price_scale, PRECISION)
    xp[j] = y  # <------------------------------------------------- Update xp.

    # ------ Tweak price_scale with good initial guess for newton_D ----------

    # Technically a swap wouldn't require to recompute D, however since we're taking
    # fees, we need to update D to reflect the new balances.
    D = staticcall MATH.newton_D(A_gamma[0], A_gamma[1], xp, y_out[1])

    price_scale = self.tweak_price(A_gamma, xp, D)

    return [dy, fee, price_scale]


@internal
def tweak_price(
    A_gamma: uint256[2],
    _xp: uint256[N_COINS],
    D: uint256,
) -> uint256:
    """
    @notice Updates price_oracle, last_price and conditionally adjusts
            price_scale. This is called whenever there is an unbalanced
            liquidity operation: _exchange, add_liquidity, or
            remove_liquidity_fixed_out.
    @dev Contains main liquidity rebalancing logic, by tweaking `price_scale`.
    @param A_gamma Array of A and gamma parameters.
    @param _xp Array of current balances.
    @param D New D value.
    @return uint256 The new price_scale.
    """

    # ---------------------------- Read storage ------------------------------

    price_oracle: uint256 = self.cached_price_oracle
    last_prices: uint256 = self.last_prices
    price_scale: uint256 = self.cached_price_scale
    rebalancing_params: uint256[3] = self._unpack_3(self.packed_rebalancing_params)
    # Contains: allowed_extra_profit, adjustment_step, ma_time. -----^

    # ------------------ Update Price Oracle if needed -----------------------

    last_timestamp: uint256 = self.last_timestamp
    alpha: uint256 = 0
    if last_timestamp < block.timestamp:  # 0th index is for price_oracle.

        #   The moving average price oracle is calculated using the last_price
        #      of the trade at the previous block, and the price oracle logged
        #              before that trade. This can happen only once per block.

        # ------------------ Calculate moving average params -----------------

        alpha = staticcall MATH.wad_exp(
            -convert(
                unsafe_div(
                    unsafe_sub(block.timestamp, last_timestamp) * 10**18,
                    rebalancing_params[2]  # <----------------------- ma_time.
                ),
                int256,
            )
        )

        # ---------------------------------------------- Update price oracles.

        # ----------------- We cap state price that goes into the EMA with
        #                                                 2 x price_scale.
        price_oracle = unsafe_div(
            min(last_prices, 2 * price_scale) * (10**18 - alpha) +
            price_oracle * alpha,  # ^-------- Cap spot price into EMA.
            10**18
        )

        self.cached_price_oracle = price_oracle
        self.last_timestamp = block.timestamp

    #  `price_oracle` is used further on to calculate its vector distance from
    # price_scale. This distance is used to calculate the amount of adjustment
    # to be done to the price_scale.
    # ------------------------------------------------------------------------

    # Here we update the spot price, please notice that this value is unsafe
    # and can be manipulated.
    self.last_prices = unsafe_div(
        staticcall MATH.get_p(_xp, D, A_gamma) * price_scale,
        10**18
    )

    # ---------- Update profit numbers without price adjustment first --------

    # `totalSupply` might change during this function call.
    total_supply: uint256 = self.totalSupply

    # ===== donation shares (time release + add_liquidity throttling) =====
    donation_shares: uint256 = self._donation_shares()

    # locked_supply contains LP shares and unreleased donations
    locked_supply: uint256 = total_supply - donation_shares

    old_virtual_price: uint256 = self.virtual_price
    xcp: uint256 = self._xcp(D, price_scale)

    virtual_price: uint256 = 10**18 * xcp // total_supply
    # Virtual price can decrease only if A and gamma are being ramped.
    # This does not imply that the virtual price will have increased at the
    # end of this function: it can still decrease if the pool rebalances.
    if virtual_price < old_virtual_price:
        # If A and gamma are being ramped, we allow the virtual price to decrease,
        # as changing the shape of the bonding curve causes losses in the pool.
        assert self._is_ramping(), "virtual price decreased"

    # xcp_profit follows growth of virtual price (and goes down on ramping)
    xcp_profit: uint256 = self.xcp_profit + virtual_price - old_virtual_price
    self.xcp_profit = xcp_profit

    # ------------ Rebalance liquidity if there's enough profits to adjust it:
    #
    # Mathematical basis for rebalancing condition:
    # 1. xcp_profit grows after virtual price, total growth since launch = (xcp_profit − 1)
    # 2. We reserve half of the growth for LPs and admin, rest is used to rebalance the pool

    # Rebalancing condition transformation:
    # virtual_price - 1 > (xcp_profit - 1)/2 + allowed_extra_profit
    # virtual_price > 1 + (xcp_profit - 1)/2 + allowed_extra_profit
    threshold_vp: uint256 = 10**18 + (xcp_profit - 10**18) // 2

    # The allowed_extra_profit parameter prevents reverting gas-wasting rebalances
    # by ensuring sufficient profit margin

    # user_supply < total_supply => vp_boosted > virtual_price
    # by not accounting for donation shares, virtual_price is boosted leading to rebalance trigger
    # this is approximate condition that preliminary indicates readiness for rebalancing
    vp_boosted: uint256 = 10**18 * xcp // locked_supply
    assert vp_boosted >= virtual_price, "negative donation"
    if vp_boosted  > threshold_vp + rebalancing_params[0]:
        #             allowed_extra_profit --------^
        norm: uint256 = unsafe_div(
            unsafe_mul(price_oracle, 10**18), price_scale
        )
        if norm > 10**18:
            norm = unsafe_sub(norm, 10**18)
        else:
            norm = unsafe_sub(10**18, norm)
        adjustment_step: uint256 = max(
            rebalancing_params[1], unsafe_div(norm, 5)
        )  #           ^------------------------------------- adjustment_step.

        # We only adjust prices if the vector distance between price_oracle
        # and price_scale is large enough. This check ensures that no rebalancing
        # occurs if the distance is low i.e. the pool prices are pegged to the
        # oracle prices.
        if norm > adjustment_step:
            # Calculate new price scale.
            p_new: uint256 = unsafe_div(
                price_scale * unsafe_sub(norm, adjustment_step) +
                adjustment_step * price_oracle,
                norm
            )  # <---- norm is non-zero and gt adjustment_step; unsafe = safe.

            # ---------------- Update stale xp (using price_scale) with p_new.

            xp: uint256[N_COINS] = [
                _xp[0],
                unsafe_div(_xp[1] * p_new, price_scale)
            ]

            # ------------------------------------------ Update D with new xp.
            new_D: uint256 = staticcall MATH.newton_D(A_gamma[0], A_gamma[1], xp, 0)
            # --------------------------------------------- Calculate new xcp.
            new_xcp: uint256 = self._xcp(new_D, p_new)
            new_virtual_price: uint256 = 10**18 * new_xcp // total_supply

            donation_shares_to_burn: uint256 = 0
            # burn donations to get to old vp, but not below threshold_vp
            goal_vp: uint256 = max(threshold_vp, virtual_price)
            if new_virtual_price < goal_vp:
                # new_virtual_price is lower than virtual_price.
                # We attempt to boost virtual_price by burning some donation shares
                # This will result in more frequent rebalances.
                #
                #   vp(0)      = xcp /  total_supply          # no burn  -> lowest vp
                #   vp(B)      = xcp / (total_supply – B)     # burn B   -> higher vp
                #
                # Goal: find the *smallest* B such that
                #        vp(B) -> virtual_price (pre-rebalance value)
                #          B   <= donation_shares

                # what would be total supply with (old) virtual_price and new_xcp
                tweaked_supply: uint256 = 10**18 * new_xcp // goal_vp
                assert tweaked_supply < total_supply, "tweaked supply must shrink"
                donation_shares_to_burn = min(
                    unsafe_sub(total_supply, tweaked_supply), # burn the difference between supplies
                    donation_shares # but not more than we can burn (lp shares donation)
                )
                # update virtual price with the tweaked total supply
                new_virtual_price = 10**18 * new_xcp // (total_supply - donation_shares_to_burn)
                # we thus burn some donation shares to compensate for virtual price drop

            if (
                new_virtual_price > 10**18 and
                new_virtual_price >= threshold_vp
                # only rebalance when pool preserves half of the profits
            ):
                self.D = new_D
                self.virtual_price = new_virtual_price
                self.cached_price_scale = p_new
                if donation_shares_to_burn > 0:
                    # we burned some donation shares, update related state
                    self.donation_shares -= donation_shares_to_burn
                    self.totalSupply -= donation_shares_to_burn
                    self.last_donation_release_ts = block.timestamp
                return p_new

    # If we end up here price_scale was not adjusted. So we update the state
    # with the virtual price and D we calculated before attempting a rebalance.
    self.D = D
    self.virtual_price = virtual_price

    return price_scale


@internal
def _claim_admin_fees():
    """
    @notice Claims admin fees and sends it to fee_receiver set in the factory.
    @dev Functionally similar to:
         1. Calculating admin's share of fees,
         2. minting LP tokens,
         3. admin claims underlying tokens via remove_liquidity.
    """

    # --------------------- Check if fees can be claimed ---------------------

    # Disable fee claiming if:
    # 1. If time passed since last fee claim is less than
    #    MIN_ADMIN_FEE_CLAIM_INTERVAL.
    # 2. Pool parameters are being ramped.

    last_claim_time: uint256 = self.last_admin_fee_claim_timestamp
    if (
        unsafe_sub(block.timestamp, last_claim_time) < MIN_ADMIN_FEE_CLAIM_INTERVAL or
        self._is_ramping()
    ):
        return

    xcp_profit: uint256 = self.xcp_profit  # <---------- Current pool profits.
    xcp_profit_a: uint256 = self.xcp_profit_a  # <- Profits at previous claim.
    current_lp_token_supply: uint256 = self.totalSupply
    # Do not claim admin fees if:
    # 1. insufficient profits accrued since last claim, and
    # 2. there are less than 10**18 (or 1 unit of) lp tokens, else it can lead
    #    to manipulated virtual prices.

    if xcp_profit <= xcp_profit_a or current_lp_token_supply < 10**18:
        return

    # ---------- Conditions met to claim admin fees: compute state. ----------
    # no _get_D() because we can't claim during ramping
    D: uint256 = self.D

    vprice: uint256 = self.virtual_price
    price_scale: uint256 = self.cached_price_scale
    fee_receiver: address = staticcall factory.fee_receiver()
    balances: uint256[N_COINS] = self.balances

    #  Admin fees are calculated as follows.
    #      1. Calculate accrued profit since last claim. `xcp_profit`
    #         is the current profits. `xcp_profit_a` is the profits
    #         at the previous claim.
    #      2. Take out admin's share, stored in self.admin_fee (with 10**10 precision).
    #      3. Since half of the profits go to rebalancing the pool, we
    #         are left with half; so divide by 2.

    fees: uint256 = unsafe_div(
        unsafe_sub(xcp_profit, xcp_profit_a) * self.admin_fee, 2 * 10**10
    )
    # ------------------------------ Claim admin fees by minting admin's share
    #                                                of the pool in LP tokens.

    admin_share: uint256 = 0
    if fee_receiver != empty(address) and fees > 0:

        # -------------------------------- Calculate admin share to be minted.
        frac: uint256 = vprice * 10**18 // (vprice - fees) - 10**18
        admin_share += current_lp_token_supply * frac // 10**18

        # When claiming fees, the virtual price decreases:
        # Let TS = total_supply, f = fees
        # vp' = xcp/(TS + TS*((vp/vp-f) - 1)) = (xcp/TS) / (1 + f/(vp-f)) =
        # = vp / (vp / (vp-f)) = (vp-f)
        # vp' = (vp-f)

        # Thus, to maintain the condition vp' - 1 > (xcp_profit' - 1)/2:
        #     xcp_profit' := xcp_profit - 2 * f
        xcp_profit -= fees * 2
        # Another way to look at it - we either track admin_claimed_xcp (=sum(fees)),
        # and always use it to calculate admin+LP reserve, or just -=2*fees in xcp_profit.
        # xcp_profit as raw value is thus should't be used in integrations!

    # ------------------- Recalculate virtual_price following admin fee claim.
    total_supply_including_admin_share: uint256 = (
        current_lp_token_supply + admin_share
    )
    vprice = (
        10**18 * self._xcp(D, price_scale) //
        total_supply_including_admin_share
    )

    # Do not claim fees if doing so causes virtual price to drop below 10**18.
    if vprice < 10**18:
        return

    # ---------------------------- Update State ------------------------------

    self.xcp_profit = xcp_profit
    self.last_admin_fee_claim_timestamp = block.timestamp

    # Since we reduce balances: virtual price goes down
    self.virtual_price = vprice

    # Adjust D after admin seemingly removes liquidity
    self.D = D - unsafe_div(D * admin_share, total_supply_including_admin_share)

    if xcp_profit > xcp_profit_a:
        self.xcp_profit_a = xcp_profit  # <-------- Cache last claimed profit.

    # --------------------------- Handle Transfers ---------------------------

    admin_tokens: uint256[N_COINS] = empty(uint256[N_COINS])
    if admin_share > 0:

        for i: uint256 in range(N_COINS):

            admin_tokens[i] = (
                balances[i] * admin_share //
                total_supply_including_admin_share
            )

            # _transfer_out tokens to admin and update self.balances. State
            # update to self.balances occurs before external contract calls:
            self._transfer_out(i, admin_tokens[i], fee_receiver)

        log ClaimAdminFee(admin=fee_receiver, tokens=admin_tokens)


@internal
@view
def _xp(
    balances: uint256[N_COINS],
    price_scale: uint256,
) -> uint256[N_COINS]:
    return [
        balances[0] * PRECISIONS[0],
        unsafe_div(balances[1] * PRECISIONS[1] * price_scale, PRECISION)
    ]

@external
@view
def user_supply() -> uint256:
    """
    @notice Returns the amount of LP tokens that are not locked in donations.
    @return uint256 Amount of LP tokens that are not locked in donations.
    """
    return self.totalSupply - self.donation_shares

@internal
@view
def _is_ramping() -> bool:
    """
    @notice Checks if A and gamma are ramping.
    @return bool True if A and/or gamma are ramping, False otherwise.
    """
    return self.future_A_gamma_time > block.timestamp

@internal
@view
def _check_admin():
    assert msg.sender == staticcall factory.admin(), "only owner"

@internal
@view
def _A_gamma() -> uint256[2]:
    t1: uint256 = self.future_A_gamma_time

    A_gamma_1: uint256 = self.future_A_gamma
    gamma1: uint256 = A_gamma_1 & 2**128 - 1
    A1: uint256 = A_gamma_1 >> 128

    if block.timestamp < t1:

        # --------------- Handle ramping up and down of A --------------------

        A_gamma_0: uint256 = self.initial_A_gamma
        t0: uint256 = self.initial_A_gamma_time

        t1 -= t0
        t0 = block.timestamp - t0
        t2: uint256 = t1 - t0

        A1 = ((A_gamma_0 >> 128) * t2 + A1 * t0) // t1
        gamma1 = ((A_gamma_0 & 2**128 - 1) * t2 + gamma1 * t0) // t1

    return [A1, gamma1]


@internal
@view
def _fee(xp: uint256[N_COINS]) -> uint256:

    # unpack mid_fee, out_fee, fee_gamma
    fee_params: uint256[3] = self._unpack_3(self.packed_fee_params)

    # warm up variable with sum of balances
    B: uint256 = xp[0] + xp[1]

    # balance indicator that goes from 10**18 (perfect pool balance) to 0 (very imbalanced, 100:1 and worse)
    # N^N * (xp[0] * xp[1]) / (xp[0] + xp[1])**2
    B = PRECISION * N_COINS**N_COINS * xp[0] // B * xp[1] // B

    # regulate slope using fee_gamma
    # fee_gamma * balance_term / (fee_gamma * balance_term + 1 - balance_term)
    B = fee_params[2] * B // (unsafe_div(fee_params[2] * B, 10**18)  + 10**18 - B)

    # mid_fee * B + out_fee * (1 - B)
    return unsafe_div(fee_params[0] * B + fee_params[1] * (10**18 - B), 10**18)


@internal
@view
def _get_D(A_gamma: uint256[2], xp: uint256[N_COINS]) -> uint256:
    # Normally we need self.D, however, if A and/or gamma are ramping,
    # we need to recalculate D using the current A and gamma values.
    if self._is_ramping():
        # ongoing ramping, recalculate D
        return staticcall MATH.newton_D(A_gamma[0], A_gamma[1], xp, 0)
    else:
        # not ramping, use self.D from storage
        return self.D


@internal
@pure
def _xcp(D: uint256, price_scale: uint256) -> uint256:
    # We compute xcp according to the formula in the whitepaper:

    # The following explanation relies on the assumption that the
    # balances have already been scaled by the price scale as shown
    # above.

    # The intuition behind this formula comes from the UniV2
    # whitepaper where the initial amount of LP tokens is set to
    # the geometric mean of the balances, in fact xcp stands for
    # x (balances) constant product.

    # Our invariant behaves in such a way that at the center of the
    # bonding curve:
    # (1) D(x, y) = D(x, x) = 2x.
    # In simple terms this mean that at the center the pool behaves exactly
    # like a constant sum AMM.
    # Here we want to treat the pool as a constant product AMM:
    # (2) xy = k (the constant product invariant).
    # (3) x^2 = k (because we are at the center of the curve where x = y).
    # (4) x = D / 2 (because D(x, y) = 2x in (1]).

    # For xp[0] the price scale is 1 (see whitepaper) so we can obtain
    # x[0] directly from [4]
    # For xp[1] the price scale is != 1 so we divide by the price scale
    # that has unit (coin0/coin1) to convert D (coin0) into xp[1] (coin1):
    # (5) x[1] = D / 2 / price_scale.

    # In the end we take the geometric average of the scaled balances:
    # xcp = sqrt(D // (N_COINS * 1) * D // (N_COINS * price_scale))
    # this is equivalent to D // N_COINS * sqrt(price_scale).
    return D * PRECISION // N_COINS // isqrt(PRECISION * price_scale)


@internal
@view
def _calc_token_fee(amounts: uint256[N_COINS],
                    xp: uint256[N_COINS],
                    donation: bool = False,
                    deposit: bool = False,
                    from_view: bool = False) -> uint256:

    if donation:
        # Donation fees are 0, but NOISE_FEE is required for numerical stability
        return NOISE_FEE

    surplus_amounts: uint256[N_COINS] = amounts
    if from_view:
        # When calling from the view contract no liquidity has been
        # added to the balances.
        surplus_amounts = [0, 0]

    # the ratio of the balances before the liquidity operation
    # balances[0] / balances[1] (adjusted for fixed precisions)
    balances_ratio: uint256 = (self.balances[0] - surplus_amounts[0]) * PRECISIONS[0] * PRECISION // ((self.balances[1] - surplus_amounts[1]) * PRECISIONS[1])
    # We calculate the fee based on the impact on the spot balances.
    # For this reason here (AND ONLY HERE) we use the balances ratio and not
    # the price_scale in self._xp().
    amounts = self._xp(amounts, balances_ratio)

    # fee = sum(amounts_i - avg(amounts)) * fee' / sum(amounts)
    # fee' = _fee(xp) * N_COINS / (4 * (N_COINS - 1)) = _fee(xp)/2 (for N_COINS=2)
    fee: uint256 = unsafe_div(
        unsafe_mul(self._fee(xp), N_COINS),
        unsafe_mul(4, unsafe_sub(N_COINS, 1))
    )

    S: uint256 = 0
    for _x: uint256 in amounts:
        S += _x

    avg: uint256 = unsafe_div(S, N_COINS)
    Sdiff: uint256 = 0

    for _x: uint256 in amounts:
        if _x > avg:
            Sdiff += unsafe_sub(_x, avg)
        else:
            Sdiff += unsafe_sub(avg, _x)

    lp_spam_penalty_fee: uint256 = 0
    if deposit:
        # Penalty fee for spamming add_liquidity into the pool
        current_expiry: uint256 = self.donation_protection_expiry_ts
        if current_expiry > block.timestamp:
            # The penalty is proportional to the remaining protection time and the current pool fee.
            protection_factor: uint256 = min((current_expiry - block.timestamp) * PRECISION // self.donation_protection_period, PRECISION)
            lp_spam_penalty_fee = protection_factor * fee // PRECISION

    return fee * Sdiff // S + NOISE_FEE + lp_spam_penalty_fee

@view
@external
def calc_withdraw_fixed_out(lp_token_amount: uint256, i: uint256, amount_i: uint256) -> uint256:
    """
    @notice Calculate the amounts of coin[1-i] that will be received for burning the lp
    tokens while specifying the amount of coin[i] to be withdrawn.
    @param lp_token_amount LP Token amount to burn.
    @param i index of the token for which the withdrawal amount is specified.
    @param amount_i exact amount of token i which will be withdrawn.
    @return uint256 Amount of token 1-i received for burning token_amount LP tokens.
    """
    return self._calc_withdraw_fixed_out(
        self._A_gamma(),
        lp_token_amount,
        i,
        amount_i,
    )[0]

@view
@external
def calc_withdraw_one_coin(lp_token_amount: uint256, i: uint256) -> uint256:
    """
    @notice Calculate how much of coin[i] will be received when withdrawing liquidity in a single coin.
    @dev This function uses the logic from _calc_withdraw_fixed_out by setting amount_i to 0.
        This forces the withdrawal to be entirely in the other coin.
    @param lp_token_amount LP Token amount to burn.
    @param i index of the token to be withdrawn
    @return uint256 Amount of coin[i] tokens received for burning token_amount LP tokens.
    """
    return self._calc_withdraw_fixed_out(
        self._A_gamma(),
        lp_token_amount,
        1 - i, # Here we flip i because we want to constrain the other coin to be zero.
        0, # We set the amount of coin[1 - i] to be withdrawn to 0.
    )[0]

@internal
@view
def _calc_withdraw_fixed_out(
    A_gamma: uint256[2],
    lp_token_amount: uint256,
    i: uint256,
    amount_i: uint256,
) -> (uint256, uint256, uint256[N_COINS], uint256):
    """
    Withdraws specified number of LP tokens while amount of coin `i` is also specified
    """

    token_supply: uint256 = self.totalSupply
    assert lp_token_amount <= token_supply, "withdraw > supply"

    # Since N_COINS = 2, we don't need to check if i < N_COINS
    # because j = 1 - i will underflow for any i > 1
    j: uint256 = 1 - i

    balances: uint256[N_COINS] = self.balances

    # -------------------------- Calculate D0 and xp -------------------------

    price_scale: uint256 = self.cached_price_scale
    xp: uint256[N_COINS] = self._xp(balances, price_scale)
    D: uint256 = self._get_D(A_gamma, xp)

    # We adjust D not to take into account any donated amount. Donations
    # should never be withdrawable by the LPs.

    # ------------------------------ Amounts calc ----------------------------
    dD: uint256 = unsafe_div(lp_token_amount * D, token_supply)
    xp_new: uint256[N_COINS] = xp

    price_scales: uint256[N_COINS] = [PRECISION * PRECISIONS[0], price_scale * PRECISIONS[1]]

    # amountsp (amounts * p) is the dx and dy amounts that the user will receive
    # after the withdrawal scaled for the price scale (p).
    amountsp: uint256[N_COINS] = empty(uint256[N_COINS])
    # This withdrawal method fixes the amount of token i to be withdrawn,
    # this is why here we don't compute amountsp[i] but we give it as a
    # constraint (after appropriate scaling).
    amountsp[i] = unsafe_div(amount_i * price_scales[i], PRECISION)
    xp_new[i] -= amountsp[i]

    # We compute the position on the y axis after a withdrawal of dD with the constraint
    # that xp_new[i] has been reduced by amountsp[i]. This is the new position on the curve
    # after the withdrawal without applying fees.
    y: uint256 = (staticcall MATH.get_y(A_gamma[0], A_gamma[1], xp_new, D - dD, j))[0]
    amountsp[j] = xp[j] - y
    xp_new[j] = y

    # _calc_token_fee expects unscaled amounts and without decimals
    # adjustments.
    amounts: uint256[N_COINS] = empty(uint256[N_COINS])
    amounts[i] = amount_i
    if i == 0:
        amounts[1] = amountsp[1] // PRECISIONS[1] * PRECISION // price_scale
    else:
        amounts[0] = amountsp[0] // PRECISIONS[0]
    # The only way to compute the fees is to simulate a withdrawal as we have done
    # above and then rewind and apply the fees.
    approx_fee: uint256 = self._calc_token_fee(amounts, xp_new)
    dD -= dD * approx_fee // 10**10 + 1

    # Same reasoning as before except now we're charging fees.
    y = (staticcall MATH.get_y(A_gamma[0], A_gamma[1], xp_new, D - dD, j))[0]
    # We descale y to obtain the amount dy in balances and not scaled balances.
    dy: uint256 = (xp[j] - y) * PRECISION // price_scales[j]
    xp_new[j] = y

    return dy, D - dD, xp_new, approx_fee


# ------------------------ ERC20 functions -----------------------------------


@internal
def _approve(_owner: address, _spender: address, _value: uint256):
    self.allowance[_owner][_spender] = _value

    log Approval(owner=_owner, spender=_spender, value=_value)


@internal
def _transfer(_from: address, _to: address, _value: uint256):
    assert _to not in [self, empty(address)], "invalid receiver"

    self.balanceOf[_from] -= _value
    self.balanceOf[_to] += _value

    log Transfer(sender=_from, receiver=_to, value=_value)


@external
def transferFrom(_from: address, _to: address, _value: uint256) -> bool:
    """
    @dev Transfer tokens from one address to another.
    @param _from address The address which you want to send tokens from
    @param _to address The address which you want to transfer to
    @param _value uint256 the amount of tokens to be transferred
    @return bool True on successul transfer. Reverts otherwise.
    """
    _allowance: uint256 = self.allowance[_from][msg.sender]
    if _allowance != max_value(uint256):
        self._approve(_from, msg.sender, _allowance - _value)

    self._transfer(_from, _to, _value)
    return True


@external
def transfer(_to: address, _value: uint256) -> bool:
    """
    @dev Transfer token for a specified address
    @param _to The address to transfer to.
    @param _value The amount to be transferred.
    @return bool True on successful transfer. Reverts otherwise.
    """
    self._transfer(msg.sender, _to, _value)
    return True


@external
def approve(_spender: address, _value: uint256) -> bool:
    """
    @notice Allow `_spender` to transfer up to `_value` amount
            of tokens from the caller's account.
    @param _spender The account permitted to spend up to `_value` amount of
                    caller's funds.
    @param _value The amount of tokens `_spender` is allowed to spend.
    @return bool Success
    """
    self._approve(msg.sender, _spender, _value)
    return True


@internal
def mint(_to: address, _value: uint256) -> bool:
    """
    @dev Mint an amount of the token and assigns it to an account.
         This encapsulates the modification of balances such that the
         proper events are emitted.
    @param _to The account that will receive the created tokens.
    @param _value The amount that will be created.
    @return bool Success.
    """
    self.totalSupply += _value
    self.balanceOf[_to] += _value

    log Transfer(sender=empty(address), receiver=_to, value=_value)
    return True


@internal
def burnFrom(_to: address, _value: uint256) -> bool:
    """
    @dev Burn an amount of the token from a given account.
    @param _to The account whose tokens will be burned.
    @param _value The amount that will be burned.
    @return bool Success.
    """
    self.totalSupply -= _value
    self.balanceOf[_to] -= _value

    log Transfer(sender=_to, receiver=empty(address), value=_value)
    return True


# ------------------------- AMM View Functions -------------------------------


@internal
@view
def internal_price_oracle() -> uint256:
    """
    @notice Returns the oracle price of the coin at index `k` w.r.t the coin
            at index 0.
    @dev The oracle is an exponential moving average, with a periodicity
         determined by `self.ma_time`. The aggregated prices are cached state
         prices (dy/dx) calculated AFTER the latest trade.
    @param k The index of the coin.
    @return uint256 Price oracle value of kth coin.
    """
    price_oracle: uint256 = self.cached_price_oracle
    price_scale: uint256 = self.cached_price_scale
    last_prices_timestamp: uint256 = self.last_timestamp

    if last_prices_timestamp < block.timestamp:  # <------------ Update moving
        #                                                   average if needed.

        last_prices: uint256 = self.last_prices
        ma_time: uint256 = self._unpack_3(self.packed_rebalancing_params)[2]
        alpha: uint256 = staticcall MATH.wad_exp(
            -convert(
                unsafe_sub(block.timestamp, last_prices_timestamp) * 10**18 // ma_time,
                int256,
            )
        )

        # ---- We cap state price that goes into the EMA with 2 x price_scale.
        return (
            min(last_prices, 2 * price_scale) * (10**18 - alpha) +
            price_oracle * alpha
        ) // 10**18

    return price_oracle


@external
@view
def fee_receiver() -> address:
    """
    @notice Returns the address of the admin fee receiver.
    @return address Fee receiver.
    """
    return staticcall factory.fee_receiver()


@external
@view
def admin() -> address:
    """
    @notice Returns the address of the pool's admin.
    @return address Admin.
    """
    return staticcall factory.admin()


@external
@view
def calc_token_amount(amounts: uint256[N_COINS], deposit: bool) -> uint256:
    """
    @notice Calculate LP tokens minted or to be burned for depositing or
            removing `amounts` of coins
    @dev Includes fee.
    @param amounts Amounts of tokens being deposited or withdrawn
    @param deposit True if it is a deposit action, False if withdrawn.
    @return uint256 Amount of LP tokens deposited or withdrawn.
    """
    return staticcall self.view_contract.calc_token_amount(amounts, deposit, self)


@external
@view
def get_dy(i: uint256, j: uint256, dx: uint256) -> uint256:
    """
    @notice Get amount of coin[j] tokens received for swapping in dx amount of coin[i]
    @dev Includes fee.
    @param i index of input token. Check pool.coins(i) to get coin address at ith index
    @param j index of output token
    @param dx amount of input coin[i] tokens
    @return uint256 Exact amount of output j tokens for dx amount of i input tokens.
    """
    return staticcall self.view_contract.get_dy(i, j, dx, self)


@external
@view
def get_dx(i: uint256, j: uint256, dy: uint256) -> uint256:
    """
    @notice Get amount of coin[i] tokens to input for swapping out dy amount
            of coin[j]
    @dev This is an approximate method, and returns estimates close to the input
         amount. Expensive to call on-chain.
    @param i index of input token. Check pool.coins(i) to get coin address at
           ith index
    @param j index of output token
    @param dy amount of input coin[j] tokens received
    @return uint256 Approximate amount of input i tokens to get dy amount of j tokens.
    """
    return staticcall self.view_contract.get_dx(i, j, dy, self)


@external
@view
@nonreentrant
def lp_price() -> uint256:
    """
    @notice Calculates the current price of the LP token w.r.t coin at the
            0th index
    @return uint256 LP price.
    """
    return 2 * self.virtual_price * isqrt(self.internal_price_oracle() * 10**18) // 10**18


@external
@view
@nonreentrant
def get_virtual_price() -> uint256:
    """
    @notice Calculates the current virtual price of the pool LP token.
    @dev Not to be confused with `self.virtual_price` which is a cached
         virtual price.
    @return uint256 Virtual Price.
    """

    return 10**18 * self._xcp(self.D, self.cached_price_scale) // self.totalSupply


@external
@view
@nonreentrant
def price_oracle() -> uint256:
    """
    @notice Returns the oracle price of the coin at index `k` w.r.t the coin
            at index 0.
    @dev The oracle is an exponential moving average, with a periodicity
         determined by `self.ma_time`. The aggregated prices are cached state
         prices (dy/dx) calculated AFTER the latest trade.
    @return uint256 Price oracle value of kth coin.
    """
    return self.internal_price_oracle()


@external
@view
@nonreentrant
def price_scale() -> uint256:
    """
    @notice Returns the price scale of the coin at index `k` w.r.t the coin
            at index 0.
    @dev Price scale determines the price band around which liquidity is
         concentrated.
    @return uint256 Price scale of coin.
    """
    return self.cached_price_scale


@external
@view
def fee() -> uint256:
    """
    @notice Returns the fee charged by the pool at current state.
    @dev Not to be confused with the fee charged at liquidity action, since
         there the fee is calculated on `xp` AFTER liquidity is added or
         removed.
    @return uint256 fee bps.
    """
    return self._fee(self._xp(self.balances, self.cached_price_scale))




@external
@view
def calc_token_fee(
    amounts: uint256[N_COINS], xp: uint256[N_COINS], donation: bool = False, deposit: bool = False
) -> uint256:
    """
    @notice Returns the fee charged on the given amounts for add_liquidity.
    @param amounts The amounts of coins being added to the pool (unscaled).
    @param xp The current balances of the pool multiplied by coin precisions.
    @param donation Whether the liquidity is a donation, if True only NOISE_FEE is charged.
    @param deposit Whether the liquidity is a deposit.
    @return uint256 Fee charged.
    """
    # last True is for from_view
    return self._calc_token_fee(amounts, xp, donation, deposit, True)


@view
@external
def A() -> uint256:
    """
    @notice Returns the current pool amplification parameter.
    @return uint256 A param.
    """
    return self._A_gamma()[0]


@view
@external
def gamma() -> uint256:
    """
    @notice Returns the current pool gamma parameter.
    @return uint256 gamma param.
    """
    return self._A_gamma()[1]


@view
@external
def mid_fee() -> uint256:
    """
    @notice Returns the current mid fee
    @return uint256 mid_fee value.
    """
    return self._unpack_3(self.packed_fee_params)[0]


@view
@external
def out_fee() -> uint256:
    """
    @notice Returns the current out fee
    @return uint256 out_fee value.
    """
    return self._unpack_3(self.packed_fee_params)[1]


@view
@external
def fee_gamma() -> uint256:
    """
    @notice Returns the current fee gamma
    @return uint256 fee_gamma value.
    """
    return self._unpack_3(self.packed_fee_params)[2]


@view
@external
def allowed_extra_profit() -> uint256:
    """
    @notice Returns the current allowed extra profit
    @return uint256 allowed_extra_profit value.
    """
    return self._unpack_3(self.packed_rebalancing_params)[0]


@view
@external
def adjustment_step() -> uint256:
    """
    @notice Returns the current adjustment step
    @return uint256 adjustment_step value.
    """
    return self._unpack_3(self.packed_rebalancing_params)[1]


@view
@external
def ma_time() -> uint256:
    """
    @notice Returns the current moving average time in seconds
    @dev To get time in seconds, the parameter is multipled by ln(2)
         One can expect off-by-one errors here.
    @return uint256 ma_time value.
    """
    return self._unpack_3(self.packed_rebalancing_params)[2] * 694 // 1000


@view
@external
def precisions() -> uint256[N_COINS]:  # <-------------- For by view contract.
    """
    @notice Returns the precisions of each coin in the pool.
    @return uint256[3] precisions of coins.
    """
    return PRECISIONS


@external
@view
def fee_calc(xp: uint256[N_COINS]) -> uint256:  # <----- For by view contract.
    """
    @notice Returns the fee charged by the pool at current state.
    @param xp The current balances of the pool multiplied by coin precisions.
    @return uint256 Fee value.
    """
    return self._fee(xp)


# ------------------------- AMM Admin Functions ------------------------------


@external
def ramp_A_gamma(
    future_A: uint256, future_gamma: uint256, future_time: uint256
):
    """
    @notice Initialise Ramping A and gamma parameter values linearly.
    @dev Only accessible by factory admin, and only
    @param future_A The future A value.
    @param future_gamma The future gamma value.
    @param future_time The timestamp at which the ramping will end.
    """
    self._check_admin()
    assert not self._is_ramping(), "ramp undergoing"
    assert future_time > block.timestamp + MIN_RAMP_TIME - 1, "ramp time<min"

    A_gamma: uint256[2] = self._A_gamma()
    initial_A_gamma: uint256 = A_gamma[0] << 128
    initial_A_gamma = initial_A_gamma | A_gamma[1]

    assert future_A > MIN_A - 1, "A<min"
    assert future_A < MAX_A + 1, "A>max"
    assert future_gamma > MIN_GAMMA - 1, "gamma<min"
    assert future_gamma < MAX_GAMMA + 1, "gamme>max"

    ratio: uint256 = 10**18 * future_A // A_gamma[0]
    assert ratio < 10**18 * MAX_PARAM_CHANGE + 1, "A change too high"
    assert ratio > 10**18 // MAX_PARAM_CHANGE - 1, "A change too low"

    ratio = 10**18 * future_gamma // A_gamma[1]
    assert ratio < 10**18 * MAX_PARAM_CHANGE + 1, "gamma change too high"
    assert ratio > 10**18 // MAX_PARAM_CHANGE - 1, "gamma change too low"

    self.initial_A_gamma = initial_A_gamma
    self.initial_A_gamma_time = block.timestamp

    future_A_gamma: uint256 = future_A << 128
    future_A_gamma = future_A_gamma | future_gamma
    self.future_A_gamma_time = future_time
    self.future_A_gamma = future_A_gamma

    log RampAgamma(
        initial_A=A_gamma[0],
        future_A=future_A,
        initial_gamma=A_gamma[1],
        future_gamma=future_gamma,
        initial_time=block.timestamp,
        future_time=future_time
    )


@external
def stop_ramp_A_gamma():
    """
    @notice Stop Ramping A and gamma parameters immediately.
    @dev Only accessible by factory admin.
    """
    self._check_admin()

    A_gamma: uint256[2] = self._A_gamma()
    current_A_gamma: uint256 = A_gamma[0] << 128
    current_A_gamma = current_A_gamma | A_gamma[1]
    self.initial_A_gamma = current_A_gamma
    self.future_A_gamma = current_A_gamma
    self.initial_A_gamma_time = block.timestamp
    self.future_A_gamma_time = block.timestamp

    # ------ Now (block.timestamp < t1) is always False, so we return saved A.

    log StopRampA(current_A=A_gamma[0], current_gamma=A_gamma[1], time=block.timestamp)


@external
@nonreentrant
def apply_new_parameters(
    _new_mid_fee: uint256,
    _new_out_fee: uint256,
    _new_fee_gamma: uint256,
    _new_allowed_extra_profit: uint256,
    _new_adjustment_step: uint256,
    _new_ma_time: uint256,
):
    """
    @notice Commit new parameters.
    @dev Only accessible by factory admin.
    @param _new_mid_fee The new mid fee.
    @param _new_out_fee The new out fee.
    @param _new_fee_gamma The new fee gamma.
    @param _new_allowed_extra_profit The new allowed extra profit.
    @param _new_adjustment_step The new adjustment step.
    @param _new_ma_time The new ma time. ma_time is time_in_seconds/ln(2).
    """
    self._check_admin()

    # ----------------------------- Set fee params ---------------------------

    new_mid_fee: uint256 = _new_mid_fee
    new_out_fee: uint256 = _new_out_fee
    new_fee_gamma: uint256 = _new_fee_gamma

    current_fee_params: uint256[3] = self._unpack_3(self.packed_fee_params)

    if new_out_fee < MAX_FEE + 1:
        assert new_out_fee > MIN_FEE - 1, "fee is out of range"
    else:
        new_out_fee = current_fee_params[1]

    if new_mid_fee > MAX_FEE:
        new_mid_fee = current_fee_params[0]
    assert new_mid_fee <= new_out_fee, "mid-fee is too high"

    if new_fee_gamma < 10**18:
        assert new_fee_gamma > 0, "fee_gamma out of range [1 .. 10**18]"
    else:
        new_fee_gamma = current_fee_params[2]

    self.packed_fee_params = self._pack_3([new_mid_fee, new_out_fee, new_fee_gamma])

    # ----------------- Set liquidity rebalancing parameters -----------------

    new_allowed_extra_profit: uint256 = _new_allowed_extra_profit
    new_adjustment_step: uint256 = _new_adjustment_step
    new_ma_time: uint256 = _new_ma_time

    current_rebalancing_params: uint256[3] = self._unpack_3(self.packed_rebalancing_params)

    if new_allowed_extra_profit > 10**18:
        new_allowed_extra_profit = current_rebalancing_params[0]

    if new_adjustment_step > 10**18:
        new_adjustment_step = current_rebalancing_params[1]

    if new_ma_time < 872542:  # <----- Calculated as: 7 * 24 * 60 * 60 / ln(2)
        assert new_ma_time > 86, "MA time should be longer than 60/ln(2)"
    else:
        new_ma_time = current_rebalancing_params[2]

    self.packed_rebalancing_params = self._pack_3(
        [new_allowed_extra_profit, new_adjustment_step, new_ma_time]
    )

    # ---------------------------------- LOG ---------------------------------

    log NewParameters(
        mid_fee=new_mid_fee,
        out_fee=new_out_fee,
        fee_gamma=new_fee_gamma,
        allowed_extra_profit=new_allowed_extra_profit,
        adjustment_step=new_adjustment_step,
        ma_time=new_ma_time
    )


@external
def set_donation_duration(duration: uint256):
    """
    @notice Set the donation duration.
    @param duration The new donation duration.
    @dev The time required for donations to fully release from locked state.
    """
    self._check_admin()
    assert duration > 0, "duration must be positive"
    self.donation_duration = duration
    log SetDonationDuration(duration=duration)


@external
def set_donation_protection_params(
    _period: uint256,
    _threshold: uint256,
):
    """
    @notice Set donation protection parameters.
    @param _period The new donation protection period in seconds.
    @param _threshold The new donation protection threshold with 10**18 precision.
    @dev _threshold = 30 * 10**18//100 means 30%
    """

    self._check_admin()
    assert _period > 0, "period must be positive"
    assert _threshold > 0, "threshold must be positive"
    self.donation_protection_period = _period
    self.donation_protection_lp_threshold = _threshold
    log SetDonationProtection(donation_protection_period=_period, donation_protection_lp_threshold=_threshold)


@external
def set_admin_fee(admin_fee: uint256):
    """
    @notice Set the admin fee.
    @param admin_fee The new admin fee.
    @dev The admin fee is a percentage of the profits that are
         claimed by the admin. The fee is set in bps.
    """

    self._check_admin()
    assert admin_fee <= MAX_ADMIN_FEE, "admin_fee>MAX"

    self.admin_fee = admin_fee
    log SetAdminFee(admin_fee=admin_fee)

@external
def set_views(views: Views):
    """
    @notice Set the view contract.
    @param views The new view contract.
    @dev This function is used to set the view contract that will be used
         to calculate the prices and fees.
    """
    self._check_admin()
    assert views != empty(Views), "views cannot be empty"
    self.view_contract = views
    log SetViews(views=views)
