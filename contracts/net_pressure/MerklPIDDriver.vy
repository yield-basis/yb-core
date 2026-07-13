# @version 0.4.3
"""
@title MerklPIDDriver
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Read-only driver that exposes the net-pressure PID *parameters* and *inputs*
        on-chain so an off-chain distributor (Merkl) can run the control loop itself.
        Merkl stores the PID state (integral, prev_pressure, filtered derivative) and,
        each step, supplies the sink pool's measured TVL plus the elapsed dt, reads the
        YB net-pressure oracle + market rate + gains from here, and gets a target APR to
        set on its campaign. The contract also converts held LT fees into a crvUSD
        reserve on trigger(); for now the reserve is exposed read-only (reserve()) - the
        approval/claim path that lets Merkl pull it is wired in a later step.
@dev Same control law as contracts/net_pressure/PID.vy, but STATELESS: this contract
     keeps no integral/derivative/clock and drives no FastGauge. preview_target_apr() is
     a pure function of (sink_tvl, prior PID state, dt) plus the live oracle/market/param
     reads; it returns the target (bonus) APR and the next PID state for Merkl to persist.
     Unlike PID.vy it sets no crvUSD/sec rate - Merkl turns the APR into a reward budget
     against the TVL it measures, and `sink` here is Merkl's measured TVL rather than an
     on-chain gauge EMA.

     Control loop, per step (dt in years), all 1e18 fixed point - identical to PID.vy
     except `sink` is Merkl-supplied:
       pressure     = max(0, sum of net pressure) / sum of half-TVL   (oracle-priced)
       sink         = sink_tvl / sum of half-TVL                      (Merkl-supplied)
       error        = pressure - sink                                 (coverage gap)
       integral    += error * dt           clamped to [0, max_integral]
       d_pressure   = (Tf*d_pressure + d(pressure)) / (Tf + dt)       (filtered slope)
       target       = min(feedforward_gain*pressure + kp*error
                          + ki*integral + kd*max(0,d_pressure), sink_cap)
       offer        = max(1, dead_band + target / sink_per_offer)     (APR multiple, >=1x)
       target_apr   = (offer - 1) * market_rate                       (0 when no sink wanted)
     Gains/params are DAO-settable storage (not constants). The reserve is just this
     contract's crvUSD balance.
"""
from ethereum.ercs import IERC20
from snekmate.auth import ownable


initializes: ownable
exports: (ownable.owner, ownable.transfer_ownership)


# Mirrors YBNetPressure.PressureTvl (returned by net_pressure_and_tvl).
struct PressureTvl:
    net_pressure: int256
    half_tvl: uint256

# The controller's live inputs, if Merkl prefers to re-implement the step from primitives
# rather than call preview_target_apr. Just the three the step consumes: pressure (already
# floored/normalized), half_tvl (to normalize Merkl's measured sink_tvl), and market_rate.
struct RawSignals:
    pressure: uint256      # max(0, sum net_pressure) / half_tvl, 1e18 (0 if half_tvl == 0)
    half_tvl: uint256      # sum of AMM half-TVL; normalizer for the caller's sink_tvl
    market_rate: uint256   # market_rate_getter.rate(), 1e18

# One stateless PID step: the target APR plus the next state for Merkl to persist.
struct AprState:
    target_apr: uint256      # bonus APR (1e18) - the target-APR Merkl should offer
    integral: int256         # next integral (persist)
    prev_pressure: uint256   # next prev_pressure (persist; == pressure this step)
    d_pressure: int256       # next filtered derivative (persist)
    pressure: uint256        # pressure this step (monitoring)
    sink: uint256            # sink this step (monitoring)


interface CryptoPool:
    def exchange(i: uint256, j: uint256, dx: uint256, min_dy: uint256, receiver: address) -> uint256: nonpayable
    def price_oracle() -> uint256: view
    def fee() -> uint256: view
    def coins(i: uint256) -> address: view

interface PriceOracle:
    def price() -> uint256: view
    def price_w() -> uint256: nonpayable

interface Factory:
    def agg() -> PriceOracle: view
    def fee_receiver() -> address: view

interface LT:
    def balanceOf(addr: address) -> uint256: view
    def withdraw(shares: uint256, min_assets: uint256, receiver: address) -> uint256: nonpayable
    def CRYPTOPOOL() -> CryptoPool: view
    def totalSupply() -> uint256: view

interface Erc20D:
    def decimals() -> uint8: view

interface NetPressureOracle:
    def net_pressure_and_tvl(lt: address, agg_price: uint256) -> PressureTvl: view

interface MarketRateGetter:
    def rate() -> uint256: view

# The real FeeDistributor (contracts/dao/FeeDistributor.vy) stores the sets as a
# DynArray[..., MAX_TOKENS][N], so its only public accessor is the element getter
# token_sets(set_id, i) -> token (no whole-array getter, no length) - see _token_set.
interface FeeDistributor:
    def current_token_set() -> uint256: view
    def token_sets(set_id: uint256, i: uint256) -> address: view

# Merkl Pull-on-Claim wrapper (the deployed PullTokenWrapper, an ERC1967 proxy over Merkl's
# audited impl). mint(amount) is holder-only and mints to the holder - this driver. We only
# touch mint() + ERC20 approve; the wrapper's internals (pulling crvUSD from us at claim) stay
# off our books.
interface MerklWrapper:
    def mint(amount: uint256): nonpayable
    def approve(spender: address, amount: uint256) -> bool: nonpayable

MAX_CAMPAIGN_DATA: constant(uint256) = 4096

# Mirrors Merkl DistributionCreator.CampaignParameters. campaign_data is opaque (built
# off-chain per Merkl's schema for the target-APR campaign type, which fetches the APR from
# this contract itself); creator/reward_token/amount are filled in by this contract.
struct CampaignParameters:
    campaign_id: bytes32
    creator: address
    reward_token: address
    amount: uint256
    campaign_type: uint32
    start_timestamp: uint32
    duration: uint32
    campaign_data: Bytes[MAX_CAMPAIGN_DATA]

interface DistributionCreator:
    def acceptConditions(): nonpayable
    def createCampaign(campaign: CampaignParameters) -> bytes32: nonpayable
    def overrideCampaign(campaign_id: bytes32, campaign: CampaignParameters): nonpayable


event Converted:
    crvusd_gained: uint256
    reserve: uint256

event SetPressureLts:
    lts: DynArray[address, MAX_POOLS]

event SetSinkPool:
    sink_pool: indexed(address)

event SetManager:
    manager: indexed(address)

event SetMerkl:
    creator: indexed(address)
    wrapper: indexed(address)

event CampaignCreated:
    campaign_id: indexed(bytes32)
    amount: uint256
    campaign_type: uint32
    duration: uint32

event CampaignOverridden:
    campaign_id: indexed(bytes32)
    duration: uint32

event SetSources:
    net_pressure: indexed(address)
    market_rate_getter: indexed(address)
    fee_distributor: indexed(address)

event SetGains:
    feedforward_gain: int256
    kp: int256
    ki: int256
    kd: int256
    max_integral: int256
    sink_cap: int256
    dead_band: uint256
    sink_per_offer: uint256
    d_filter_time: uint256

event SetExecutionParams:
    swap_fee_multiplier: uint256
    dust_floor: uint256

event Recover:
    token: indexed(address)
    amount: uint256


PRECISION: constant(uint256) = 10**18
PRECISION_SIGNED: constant(int256) = 10**18   # 1e18 for the controller's int256 fixed-point
FEE_DENOM: constant(uint256) = 10**10   # Curve pool fee() is scaled to 1e10
SECONDS_PER_YEAR: constant(uint256) = 365 * 86400
MAX_POOLS: constant(uint256) = 20
MAX_TOKENS: constant(uint256) = 100   # must match FeeDistributor.MAX_TOKENS

# Generous magnitude ceilings on the DAO-set params: a safety rail keeping the controller's
# products well inside int256 so no configuration can make a view overflow-revert. Set far
# above any sane tuned value, so they never constrain real tuning.
MAX_PARAM: constant(uint256) = 10**24
MAX_PARAM_SIGNED: constant(int256) = 10**24
MAX_FILTER_TIME: constant(uint256) = 10**9   # ~31.7 yr ceiling on the derivative filter Tf (s)

CRVUSD: public(immutable(IERC20))
FACTORY: public(immutable(Factory))   # owns the crvUSD aggregator config (Factory.agg)

# Wiring (DAO-settable)
net_pressure: public(NetPressureOracle)
market_rate_getter: public(MarketRateGetter)
fee_distributor: public(FeeDistributor)
pressure_lts: public(DynArray[address, MAX_POOLS])
# Informational only: the stableswap pool whose TVL Merkl measures and feeds back as
# `sink_tvl`. Never read on-chain (Merkl supplies the number); stored so the sink pool is
# discoverable from the driver.
sink_pool: public(address)

# Optional operator role: may set_gains() and set up the Merkl campaign. The DAO (owner) can
# do all of that too; setting manager to empty(address) makes those actions DAO-only.
manager: public(address)

# Merkl wiring (DAO-settable): the DistributionCreator we create/override campaigns on, and the
# whitelisted Pull-on-Claim wrapper (crvUSD underlying, this contract as holder) used as the
# campaign reward token. Zero until the DAO installs them via set_merkl.
merkl_creator: public(DistributionCreator)
reward_wrapper: public(MerklWrapper)

# Controller params (1e18; signed where they can multiply a signed term)
feedforward_gain: public(int256)   # alpha
kp: public(int256)
ki: public(int256)
kd: public(int256)
max_integral: public(int256)       # integral clamp (>=0)
sink_cap: public(int256)           # target-sink clamp (>=0)
dead_band: public(uint256)
sink_per_offer: public(uint256)    # beta
d_filter_time: public(uint256)     # derivative low-pass filter time constant Tf (s)
swap_fee_multiplier: public(uint256)  # min_dy = oracle * (1 - mult*pool_fee)
dust_floor: public(uint256)        # skip converting LT balances below this

# cryptopool -> its asset already given an infinite approval (approve once, then skip).
pool_approved: public(HashMap[CryptoPool, bool])


@deploy
def __init__(crvusd: IERC20, factory: Factory, net_pressure: NetPressureOracle,
             market_rate_getter: MarketRateGetter, fee_distributor: FeeDistributor, owner: address):
    """
    @notice Deploy the driver with default gains (DAO can retune).
    @param crvusd The crvUSD token: the reserve and reward asset.
    @param factory The YB Factory: the owner of the crvUSD aggregator config.
    @param net_pressure The YBNetPressure oracle (net pressure + oracle TVL).
    @param market_rate_getter Source of the market rate the offer is quoted against.
    @param fee_distributor FeeDistributor whose token set lists the LT fees to convert.
    @param owner DAO address that owns the configuration and reserve.
    """
    ownable.__init__()
    ownable._transfer_ownership(owner)
    CRVUSD = crvusd
    FACTORY = factory
    self.net_pressure = net_pressure
    self.market_rate_getter = market_rate_getter
    self.fee_distributor = fee_distributor

    # Default gains, matching PID.vy; DAO can retune.
    self.feedforward_gain = 1_160_000_000_000_000_000      # 1.16
    self.kp = 50 * 10**18
    self.ki = 1988 * 10**18
    self.kd = 49_000_000_000_000_000                       # 0.049
    self.max_integral = 2_930_000_000_000_000_000          # 2.93
    self.sink_cap = 22 * 10**18
    self.dead_band = 1_600_000_000_000_000_000             # 1.6
    self.sink_per_offer = 500_000_000_000_000_000          # 0.5
    self.d_filter_time = 6 * 3600                          # 6h derivative filter (Tf)
    self.swap_fee_multiplier = 3 * 10**18 // 2             # 1.5
    self.dust_floor = 10**12


# --- fee conversion (builds the crvUSD reserve) ------------------------------

@internal
def _ensure_pool_approval(pool: CryptoPool, asset: IERC20):
    """Grant `pool` an infinite allowance for its `asset` once, then remember it."""
    if not self.pool_approved[pool]:
        assert extcall asset.approve(pool.address, max_value(uint256), default_return_value=True)
        self.pool_approved[pool] = True


@internal
@view
def _token_set() -> DynArray[address, MAX_TOKENS]:
    """Read the FeeDistributor's current token set. Its `token_sets` is a DynArray[][], so
    the only getter is token_sets(set_id, i) -> token (no whole-array getter, no length) -
    enumerate by index until the bounds check reverts."""
    fd: address = self.fee_distributor.address
    set_id: uint256 = staticcall self.fee_distributor.current_token_set()
    out: DynArray[address, MAX_TOKENS] = []
    for i: uint256 in range(MAX_TOKENS):
        success: bool = False
        response: Bytes[32] = b""
        success, response = raw_call(
            fd,
            abi_encode(set_id, i, method_id=method_id("token_sets(uint256,uint256)")),
            max_outsize=32, is_static_call=True, revert_on_failure=False)
        if not success:
            break  # index past the end of the DynArray -> bounds check reverted
        out.append(abi_decode(response, address))
    return out


@internal
def _convert_fees(agg_price: uint256):
    """
    @notice Convert any held LT fees into crvUSD.
    @dev For each LT in the FeeDistributor token set, withdraw its asset then swap to
         crvUSD in that LT's cryptopool. Both legs are bounded by the same
         manipulation-resistant discount (swap_fee_multiplier * pool fee): the withdraw by
         the price_oracle-fair value of the shares (half_tvl-based), the swap by the
         price_oracle. The swap is best-effort - a pool that can't meet its min_dy is skipped
         rather than reverting the whole trigger. Same logic as PID._convert_fees, minus the cache.
    @param agg_price The crvUSD aggregator price (1e18), read once per trigger.
    """
    token_set: DynArray[address, MAX_TOKENS] = self._token_set()
    for lt_addr: address in token_set:
        lt: LT = LT(lt_addr)
        shares: uint256 = staticcall lt.balanceOf(self)
        if shares < self.dust_floor:
            continue
        pool: CryptoPool = staticcall lt.CRYPTOPOOL()
        asset: IERC20 = IERC20(staticcall pool.coins(1))
        p_o: uint256 = staticcall pool.price_oracle()
        # pool.fee() is scaled to FEE_DENOM (1e10); discount is rescaled to 1e18 and
        # capped at PRECISION so a large multiplier/fee floors the min at 0, not underflow.
        discount: uint256 = min(self.swap_fee_multiplier * (staticcall pool.fee()) // FEE_DENOM, PRECISION)

        pt: PressureTvl = staticcall self.net_pressure.net_pressure_and_tvl(lt_addr, agg_price)

        # 1) Withdraw, bounded by the price_oracle-fair value of the shares:
        #    half_tvl * shares/totalSupply / price_oracle, in the asset's own decimals.
        precision1: uint256 = 10 ** (18 - convert(staticcall Erc20D(asset.address).decimals(), uint256))
        fair_assets: uint256 = pt.half_tvl * shares // (staticcall lt.totalSupply()) * PRECISION // p_o // precision1
        min_assets: uint256 = fair_assets * (PRECISION - discount) // PRECISION
        asset_out: uint256 = extcall lt.withdraw(shares, min_assets, self)
        if asset_out == 0:
            continue
        # 2) Swap asset -> crvUSD, bounded by the EMA price minus the same discount. Best-effort:
        #    a pool that can't meet min_dy (EMA gap wider than the discount) is skipped instead of
        #    reverting the trigger, so one stuck pool can't block the rest. The withdrawn asset then
        #    stays in the reserve (recoverable) until it can be converted.
        min_dy: uint256 = asset_out * p_o // PRECISION * (PRECISION - discount) // PRECISION
        self._ensure_pool_approval(pool, asset)
        swapped: bool = raw_call(
            pool.address,
            abi_encode(convert(1, uint256), convert(0, uint256), asset_out, min_dy, self,
                       method_id=method_id("exchange(uint256,uint256,uint256,uint256,address)")),
            max_outsize=0, revert_on_failure=False)  # coin1 (asset) -> coin0 (crvUSD)


@external
@nonreentrant
def trigger():
    """
    @notice Convert held LT fees into the crvUSD reserve. Permissionless; the FeeSplitter
            calls it after forwarding this driver's share. Does NOT touch the controller -
            the PID loop lives off-chain (see preview_target_apr).
    """
    reserve_before: uint256 = staticcall CRVUSD.balanceOf(self)
    # One crvUSD aggregator read shared by the whole trigger; price_w checkpoints its EMA.
    agg_price: uint256 = extcall (staticcall FACTORY.agg()).price_w()
    self._convert_fees(agg_price)
    reserve_after: uint256 = staticcall CRVUSD.balanceOf(self)
    log Converted(crvusd_gained=reserve_after - reserve_before, reserve=reserve_after)


# --- read-only surface for Merkl ---------------------------------------------

@internal
@view
def _sum_pressure(agg_price: uint256) -> (int256, uint256):
    """Aggregate net pressure and half-TVL over pressure_lts (crvUSD numeraire)."""
    net: int256 = 0
    half_tvl: uint256 = 0
    for lt: address in self.pressure_lts:
        pt: PressureTvl = staticcall self.net_pressure.net_pressure_and_tvl(lt, agg_price)
        net += pt.net_pressure
        half_tvl += pt.half_tvl
    return net, half_tvl


@external
@view
def raw_signals() -> RawSignals:
    """
    @notice The controller's live primitive inputs, for Merkl to re-implement the step
            (or for monitoring). `sink` is not here: it is Merkl's measured TVL.
    @return RawSignals(pressure, half_tvl, market_rate).
    """
    agg_price: uint256 = staticcall (staticcall FACTORY.agg()).price()
    net: int256 = 0
    half_tvl: uint256 = 0
    net, half_tvl = self._sum_pressure(agg_price)
    pressure: uint256 = 0
    if half_tvl > 0 and net > 0:
        pressure = convert(net, uint256) * PRECISION // half_tvl
    return RawSignals(pressure=pressure, half_tvl=half_tvl,
                      market_rate=staticcall self.market_rate_getter.rate())


@external
@view
def preview_target_apr(sink_tvl: uint256, integral_in: int256, prev_pressure_in: uint256,
                       d_pressure_in: int256, dt: uint256) -> AprState:
    """
    @notice Run one stateless PID step and return the target APR plus the next state.
            Merkl persists (integral, prev_pressure, d_pressure) and the wall time of the
            call; each step it passes back the stored state, its measured `sink_tvl`, and
            `dt` = seconds since its previous call. Reads pressure/market/params live here.
    @dev Authoritative on-chain twin of the Python reference in
         scripts/merkl_pid_driver.py; both must agree bit-for-bit. On the first connected
         step Merkl should pass integral_in=0, d_pressure_in=0, prev_pressure_in=pressure
         and dt=0 (a clean slate), exactly as PID.vy restarts on connect.
    @param sink_tvl Merkl's measured sink-pool TVL in crvUSD (1e18).
    @param integral_in Stored integral accumulator (1e18, >=0).
    @param prev_pressure_in Stored pressure from the previous step (1e18).
    @param d_pressure_in Stored filtered derivative (1e18, signed).
    @param dt Seconds since the previous step.
    @return AprState(target_apr, integral, prev_pressure, d_pressure, pressure, sink).
    """
    agg_price: uint256 = staticcall (staticcall FACTORY.agg()).price()
    net: int256 = 0
    half_tvl: uint256 = 0
    net, half_tvl = self._sum_pressure(agg_price)
    assert half_tvl > 0, "No pools"

    pressure: uint256 = 0
    if net > 0:
        pressure = convert(net, uint256) * PRECISION // half_tvl
    sink: uint256 = sink_tvl * PRECISION // half_tvl

    dt_years: int256 = convert(dt * PRECISION // SECONDS_PER_YEAR, int256)
    error: int256 = convert(pressure, int256) - convert(sink, int256)

    integral: int256 = integral_in + error * dt_years // PRECISION_SIGNED
    integral = max(0, min(integral, self.max_integral))

    # Filtered derivative (Åström discrete form, dt & Tf in years):
    #   d[k] = (Tf*d[k-1] + Δpressure) / (Tf + dt). Tf > 0 keeps it finite when dt == 0.
    tf_years: int256 = convert(self.d_filter_time * PRECISION // SECONDS_PER_YEAR, int256)
    dp: int256 = convert(pressure, int256) - convert(prev_pressure_in, int256)
    d_pressure: int256 = (tf_years * d_pressure_in // PRECISION_SIGNED + dp) * PRECISION_SIGNED // (tf_years + dt_years)

    target: int256 = (self.feedforward_gain * convert(pressure, int256) // PRECISION_SIGNED
                      + self.kp * error // PRECISION_SIGNED
                      + self.ki * integral // PRECISION_SIGNED
                      + self.kd * max(0, d_pressure) // PRECISION_SIGNED)
    target = min(target, self.sink_cap)

    offer_signed: int256 = (convert(self.dead_band, int256)
                            + target * PRECISION_SIGNED // convert(self.sink_per_offer, int256))
    offer_multiple: uint256 = convert(max(offer_signed, PRECISION_SIGNED), uint256)
    market_rate: uint256 = staticcall self.market_rate_getter.rate()
    target_apr: uint256 = 0
    if offer_multiple > PRECISION:
        target_apr = (offer_multiple - PRECISION) * market_rate // PRECISION

    return AprState(target_apr=target_apr, integral=integral, prev_pressure=pressure,
                    d_pressure=d_pressure, pressure=pressure, sink=sink)


@external
@view
def reserve() -> uint256:
    """@notice crvUSD currently held (available to be claimed once the claim path is wired)."""
    return staticcall CRVUSD.balanceOf(self)


@external
@view
def connected() -> bool:
    """True iff the DAO has installed OUR FeeSplitter as the Factory fee_receiver - i.e. the
    live fee route is a contract whose pid() points back at this driver. Merkl uses it to
    know when to start integrating (so it never winds the integral over the pre-connection
    window). Mirrors PID._connected(); cannot be spoofed (only the DAO installs fee_receiver)."""
    fr: address = staticcall FACTORY.fee_receiver()
    if fr == empty(address):
        return False
    success: bool = False
    response: Bytes[32] = b""
    success, response = raw_call(fr, method_id("pid()"), max_outsize=32,
                                 is_static_call=True, revert_on_failure=False)
    return success and len(response) == 32 and abi_decode(response, address) == self


# --- DAO / manager control ---------------------------------------------------

@internal
@view
def _check_owner_or_manager():
    """Allow the DAO (owner) or the manager role. A zero manager is DAO-only, since
    msg.sender can never be empty(address)."""
    assert msg.sender == ownable.owner or msg.sender == self.manager, "Not manager"


@external
def set_manager(manager: address):
    """
    @notice Set (or clear) the manager role. DAO (owner) only.
    @dev The manager may set_gains() and (later) set up the Merkl campaign. Set to
         empty(address) to disable the role, making those actions DAO-only.
    @param manager The new manager, or empty(address) to disable the role.
    """
    ownable._check_owner()
    self.manager = manager
    log SetManager(manager=manager)


# --- Merkl campaigns ---------------------------------------------------------

@external
def set_merkl(creator: DistributionCreator, wrapper: MerklWrapper):
    """
    @notice Install Merkl's DistributionCreator + the whitelisted Pull-on-Claim wrapper, and
            grant the two allowances the flow needs. DAO (owner) only.
    @dev crvUSD is approved to the wrapper (so it can pull crvUSD from us at claim, and the fee
         at creation); the wrapper is approved to the creator (so createCampaign can pull the
         minted wrapper). Re-callable to migrate either address: the previously-installed pair's
         infinite allowances are first revoked to 0, so a replaced wrapper/creator keeps no pull
         on the reserve. That also stops an old wrapper from settling any still-live old-wrapper
         campaigns, so migrate only once those are wound down. Pass empty(address) for either to
         unset it - no allowance is granted to the zero address (revoking without re-granting).
    @param creator The Merkl DistributionCreator (0x8BB4C975... on mainnet), or 0x0 to unset.
    @param wrapper The PullTokenWrapperAllowImmutable (crvUSD underlying, this contract holder),
           or 0x0 to unset (which skips the crvUSD approval entirely).
    """
    ownable._check_owner()
    old_creator: address = self.merkl_creator.address
    old_wrapper: MerklWrapper = self.reward_wrapper
    old_wrapper_addr: address = old_wrapper.address
    if old_wrapper_addr != empty(address):            # revoke the previously-installed pair
        assert extcall CRVUSD.approve(old_wrapper_addr, 0, default_return_value=True)
        assert extcall old_wrapper.approve(old_creator, 0, default_return_value=True)
    self.merkl_creator = creator
    self.reward_wrapper = wrapper
    new_creator: address = creator.address
    new_wrapper: address = wrapper.address
    if new_wrapper != empty(address):                 # empty(address) unsets - never approve 0x0
        assert extcall CRVUSD.approve(new_wrapper, max_value(uint256), default_return_value=True)
        if new_creator != empty(address):
            assert extcall wrapper.approve(new_creator, max_value(uint256), default_return_value=True)
    log SetMerkl(creator=new_creator, wrapper=new_wrapper)


@external
def accept_conditions():
    """
    @notice Accept Merkl's terms on the DistributionCreator so createCampaign's `hasSigned` gate
            passes. DAO or manager. Re-call if Merkl updates its terms (messageHash).
    """
    self._check_owner_or_manager()
    extcall self.merkl_creator.acceptConditions()


@external
def create_campaign(amount: uint256, campaign_type: uint256, start_timestamp: uint256,
                    duration: uint256, campaign_data: Bytes[MAX_CAMPAIGN_DATA]) -> bytes32:
    """
    @notice Mint `amount` wrapper and open a Merkl campaign funded by it (reward token = wrapper,
            creator = this contract). crvUSD leaves our reserve only as users claim (plus the
            Merkl fee, pulled at creation). DAO or manager.
    @dev campaign_data is forwarded opaquely (built off-chain per Merkl's schema for the target-
         APR type, which fetches the APR from this contract). `amount` is the distributable cap
         and the fee scales with it, so size it to a sensible budget. Reverts if Merkl rejects
         (wrapper not whitelisted, amount too low for the duration, conditions not accepted, ...).
    @param amount Wrapper (== crvUSD, 1:1) cap for the campaign.
    @param campaign_type Merkl campaign type id.
    @param start_timestamp Campaign start (unix seconds); 0 defaults to block.timestamp (start now).
    @param duration Campaign duration in seconds.
    @param campaign_data Opaque Merkl campaign config.
    @return The Merkl campaign id.
    """
    self._check_owner_or_manager()
    wrapper: MerklWrapper = self.reward_wrapper
    extcall wrapper.mint(amount)                       # PullTokenWrapper mints to the holder (us)
    ct: uint32 = convert(campaign_type, uint32)
    dur: uint32 = convert(duration, uint32)
    # 0 -> start now. Merkl accepts a zero/stale start on-chain, but that is an already-ended window;
    # default to block.timestamp (as Merkl's own middleman does) so a delayed tx still starts "now".
    start: uint32 = convert(start_timestamp, uint32) if start_timestamp != 0 else convert(block.timestamp, uint32)
    camp: CampaignParameters = CampaignParameters(
        campaign_id=empty(bytes32),
        creator=self,
        reward_token=wrapper.address,
        amount=amount,
        campaign_type=ct,
        start_timestamp=start,
        duration=dur,
        campaign_data=campaign_data,
    )
    campaign_id: bytes32 = extcall self.merkl_creator.createCampaign(camp)
    log CampaignCreated(campaign_id=campaign_id, amount=amount, campaign_type=ct, duration=dur)
    return campaign_id


@external
def override_campaign(campaign_id: bytes32, campaign_type: uint256, start_timestamp: uint256,
                     duration: uint256, campaign_data: Bytes[MAX_CAMPAIGN_DATA]):
    """
    @notice Update a live campaign's data/duration/start. DAO or manager. Merkl keeps amount,
            creator, reward token and id immutable; the target APR is fetched by the campaign
            itself, so this is for adjusting the rules or extending, not the APR.
    @param campaign_id The campaign to override.
    @param campaign_type Merkl campaign type id.
    @param start_timestamp New start (only effective before the campaign started); 0 defaults to block.timestamp.
    @param duration New duration in seconds.
    @param campaign_data New opaque Merkl campaign config.
    """
    self._check_owner_or_manager()
    dur: uint32 = convert(duration, uint32)
    start: uint32 = convert(start_timestamp, uint32) if start_timestamp != 0 else convert(block.timestamp, uint32)
    # creator/reward_token/amount are overwritten by Merkl to the stored campaign's values.
    camp: CampaignParameters = CampaignParameters(
        campaign_id=campaign_id,
        creator=self,
        reward_token=self.reward_wrapper.address,
        amount=0,
        campaign_type=convert(campaign_type, uint32),
        start_timestamp=start,
        duration=dur,
        campaign_data=campaign_data,
    )
    extcall self.merkl_creator.overrideCampaign(campaign_id, camp)
    log CampaignOverridden(campaign_id=campaign_id, duration=dur)


@external
def recover(token: IERC20, amount: uint256, to: address):
    """
    @notice Sweep the crvUSD reserve (or any token) out, e.g. by DAO vote.
    @param token Token to sweep.
    @param amount Amount to transfer.
    @param to Recipient.
    """
    ownable._check_owner()
    assert extcall token.transfer(to, amount, default_return_value=True)
    log Recover(token=token.address, amount=amount)


@external
def set_pressure_lts(lts: DynArray[address, MAX_POOLS]):
    """
    @notice Set the LT markets whose net pressure is summed by the controller.
    @dev DAO only.
    @param lts The LT (market) addresses to aggregate net pressure over.
    """
    ownable._check_owner()
    self.pressure_lts = lts
    log SetPressureLts(lts=lts)


@external
def set_sink_pool(sink_pool: address):
    """
    @notice Record the stableswap pool whose TVL Merkl measures as the controller's sink.
    @dev DAO only. Informational: never read on-chain (Merkl supplies the measured TVL).
    @param sink_pool The Curve stableswap pool address.
    """
    ownable._check_owner()
    self.sink_pool = sink_pool
    log SetSinkPool(sink_pool=sink_pool)


@external
def set_sources(net_pressure: NetPressureOracle, market_rate_getter: MarketRateGetter,
                fee_distributor: FeeDistributor):
    """
    @notice Set the controller's data sources.
    @dev DAO only.
    @param net_pressure The YBNetPressure oracle (net pressure + oracle TVL).
    @param market_rate_getter Source of the market rate the offer is quoted against.
    @param fee_distributor FeeDistributor whose token set lists the LT fees to convert.
    """
    ownable._check_owner()
    self.net_pressure = net_pressure
    self.market_rate_getter = market_rate_getter
    self.fee_distributor = fee_distributor
    log SetSources(net_pressure=net_pressure.address, market_rate_getter=market_rate_getter.address,
                   fee_distributor=fee_distributor.address)


@external
def set_gains(feedforward_gain: int256, kp: int256, ki: int256, kd: int256,
              max_integral: int256, sink_cap: int256, dead_band: uint256, sink_per_offer: uint256,
              d_filter_time: uint256):
    """
    @notice Set the controller gains and clamps (all 1e18-scaled except d_filter_time).
    @dev DAO (owner) or manager. Every param is magnitude-bounded so no downstream reader
         overflows int256. Requires max_integral >= 0, sink_cap >= 0, sink_per_offer > 0,
         0 < d_filter_time.
    @param feedforward_gain Proportional gain on the raw pressure.
    @param kp Proportional gain on the coverage error (pressure - sink).
    @param ki Integral gain on the coverage error.
    @param kd Derivative gain on rising pressure.
    @param max_integral Clamp on the integral accumulator (anti-windup).
    @param sink_cap Clamp on the target sink.
    @param dead_band Offered APR multiple at zero target sink.
    @param sink_per_offer Target sink drawn per unit of offer above the dead band.
    @param d_filter_time Derivative low-pass filter time constant Tf (seconds); must be > 0
           (it is the derivative denominator floor, so a same-block dt==0 step stays finite).
    """
    self._check_owner_or_manager()
    assert abs(feedforward_gain) <= MAX_PARAM_SIGNED and abs(kp) <= MAX_PARAM_SIGNED
    assert abs(ki) <= MAX_PARAM_SIGNED and abs(kd) <= MAX_PARAM_SIGNED
    assert max_integral >= 0 and max_integral <= MAX_PARAM_SIGNED
    assert sink_cap >= 0 and sink_cap <= MAX_PARAM_SIGNED
    assert dead_band <= MAX_PARAM
    assert sink_per_offer > 0 and sink_per_offer <= MAX_PARAM
    assert d_filter_time > 0 and d_filter_time <= MAX_FILTER_TIME
    self.feedforward_gain = feedforward_gain
    self.kp = kp
    self.ki = ki
    self.kd = kd
    self.max_integral = max_integral
    self.sink_cap = sink_cap
    self.dead_band = dead_band
    self.sink_per_offer = sink_per_offer
    self.d_filter_time = d_filter_time
    log SetGains(feedforward_gain=feedforward_gain, kp=kp, ki=ki, kd=kd, max_integral=max_integral,
                 sink_cap=sink_cap, dead_band=dead_band, sink_per_offer=sink_per_offer,
                 d_filter_time=d_filter_time)


@external
def set_execution_params(swap_fee_multiplier: uint256, dust_floor: uint256):
    """
    @notice Set fee-conversion parameters (the swap discount and dust floor).
    @dev DAO (owner) or manager.
    @param swap_fee_multiplier Slippage multiplier (1e18); min_dy = oracle*(1 -
           multiplier*pool_fee). Bounded by MAX_PARAM so swap_fee_multiplier*pool.fee()
           cannot overflow before the discount is capped at PRECISION.
    @param dust_floor LT balance below which fee conversion is skipped.
    """
    self._check_owner_or_manager()
    assert swap_fee_multiplier <= MAX_PARAM
    self.swap_fee_multiplier = swap_fee_multiplier
    self.dust_floor = dust_floor
    log SetExecutionParams(swap_fee_multiplier=swap_fee_multiplier, dust_floor=dust_floor)
