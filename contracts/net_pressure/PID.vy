# @version 0.4.3
"""
@title PID (net-pressure incentive controller)
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Converts LT fees into a crvUSD reserve and runs a PID control loop on the
        aggregate crvUSD net pressure of a DAO-selected set of YB pools. The output
        is a crvUSD/second reward rate set on a FastGauge, which incentivizes
        deposits into a "sink" Curve stableswap pool that relieves the pressure.
@dev Control loop, per step (dt in years), all 1e18 fixed point:
       pressure     = max(0, sum of net pressure) / sum of half-TVL   (oracle-priced)
       sink         = sink-pool TVL / sum of half-TVL
       error        = pressure - sink                                 (coverage gap)
       integral    += error * dt           clamped to [0, max_integral]
       d_pressure   = max(0, d(pressure)/dt)                          (rising only)
       target_sink  = clip(feedforward_gain*pressure + kp*error
                           + ki*integral + kd*d_pressure, 0, sink_cap)
       offer        = dead_band + target_sink / sink_per_offer        (APR multiple)
       bonus_apr    = (offer - 1) * market_rate
       rate         = bonus_apr * staked_value / seconds_per_year     (crvUSD/sec)
     Gains/params are DAO-settable storage (not constants). The reserve is just this
     contract's crvUSD balance; the FastGauge pulls from it at checkpoint, so
     depletion simply stops the stream.
"""
from ethereum.ercs import IERC20
from snekmate.auth import ownable


initializes: ownable
exports: (ownable.owner, ownable.transfer_ownership)


interface CryptoPool:
    def exchange(i: uint256, j: uint256, dx: uint256, min_dy: uint256, receiver: address) -> uint256: nonpayable
    def price_oracle() -> uint256: view
    def fee() -> uint256: view
    def coins(i: uint256) -> address: view

interface LT:
    def balanceOf(addr: address) -> uint256: view
    def withdraw(shares: uint256, min_assets: uint256, receiver: address) -> uint256: nonpayable
    def CRYPTOPOOL() -> CryptoPool: view

interface NetPressureOracle:
    def net_pressure_oracle(lt: address) -> int256: view
    def pool_tvl_oracle(lt: address) -> uint256: view

interface MarketRateGetter:
    def rate() -> uint256: view

interface StableswapPool:
    def totalSupply() -> uint256: view
    def get_virtual_price() -> uint256: view

interface FastGauge:
    def set_reward_rate(rate: uint256): nonpayable
    def totalAssets() -> uint256: view

interface FeeDistributor:
    def current_token_set() -> uint256: view
    def token_sets(i: uint256) -> DynArray[address, MAX_TOKENS]: view


event Trigger:
    pressure: uint256
    sink: uint256
    bonus_apr: uint256
    rate: uint256

event SetParams: pass
event Recover:
    token: indexed(address)
    amount: uint256


PRECISION: constant(uint256) = 10**18
FEE_DENOM: constant(uint256) = 10**10   # Curve pool fee() is scaled to 1e10
SECONDS_PER_YEAR: constant(uint256) = 365 * 86400
MAX_POOLS: constant(uint256) = 20
MAX_TOKENS: constant(uint256) = 100   # must match FeeDistributor.MAX_TOKENS

CRVUSD: public(immutable(IERC20))

# Wiring (DAO-settable)
net_pressure: public(NetPressureOracle)
market_rate_getter: public(MarketRateGetter)
fee_distributor: public(FeeDistributor)
gauge: public(FastGauge)
sink_pool: public(StableswapPool)
pressure_lts: public(DynArray[address, MAX_POOLS])

# Controller params (1e18; signed where they can multiply a signed term)
feedforward_gain: public(int256)   # alpha
kp: public(int256)
ki: public(int256)
kd: public(int256)
max_integral: public(int256)       # integral clamp (>=0)
sink_cap: public(int256)           # target-sink clamp (>=0)
dead_band: public(uint256)
sink_per_offer: public(uint256)    # beta
swap_fee_multiplier: public(uint256)  # min_dy = oracle * (1 - mult*pool_fee)
min_interval: public(uint256)
dust_floor: public(uint256)        # skip converting LT balances below this

# State
integral: public(int256)
prev_pressure: public(uint256)
last_ts: public(uint256)

# token -> spender -> already granted an infinite approval. Lets us approve once and
# then skip the approve extcall (and its storage write) on every later conversion.
is_approved: public(HashMap[address, HashMap[address, bool]])


@deploy
def __init__(crvusd: IERC20, net_pressure: NetPressureOracle, market_rate_getter: MarketRateGetter,
             fee_distributor: FeeDistributor, owner: address):
    """
    @notice Deploy the controller with default gains (DAO can retune).
    @param crvusd The crvUSD token: the reserve and reward asset.
    @param net_pressure The YBNetPressure oracle (net pressure + oracle TVL).
    @param market_rate_getter Source of the market rate the offer is quoted against.
    @param fee_distributor FeeDistributor whose token set lists the LT fees to convert.
    @param owner DAO address that owns the configuration and reserve.
    """
    ownable.__init__()
    ownable._transfer_ownership(owner)
    CRVUSD = crvusd
    self.net_pressure = net_pressure
    self.market_rate_getter = market_rate_getter
    self.fee_distributor = fee_distributor

    # Default gains, tuned offline against historical net pressure; DAO can retune.
    self.feedforward_gain = 1_160_000_000_000_000_000      # 1.16
    self.kp = 50 * 10**18
    self.ki = 1988 * 10**18
    self.kd = 15_800_000_000_000_000                       # 0.0158
    self.max_integral = 2_930_000_000_000_000_000          # 2.93
    self.sink_cap = 22 * 10**18
    self.dead_band = 1_600_000_000_000_000_000             # 1.6
    self.sink_per_offer = 500_000_000_000_000_000          # 0.5
    self.swap_fee_multiplier = 3 * 10**18 // 2             # 1.5
    self.min_interval = 3600
    self.dust_floor = 10**12
    self.last_ts = block.timestamp


# --- fee conversion ----------------------------------------------------------

@internal
def _ensure_approval(token: IERC20, spender: address):
    """
    @notice Grant `spender` an infinite allowance for `token` once, then remember it.
    @dev Skips the approve extcall (and its storage write) on subsequent calls, so
         normal operation pays only an SLOAD instead of an approve every time.
    @param token Token to approve.
    @param spender Address allowed to pull `token` (the relevant Curve pool/gauge).
    """
    if not self.is_approved[token.address][spender]:
        assert extcall token.approve(spender, max_value(uint256), default_return_value=True)
        self.is_approved[token.address][spender] = True


@internal
def _convert_fees():
    """
    @notice Convert any held LT fees into crvUSD.
    @dev For each LT in the FeeDistributor token set, withdraw its asset then swap to
         crvUSD in that LT's cryptopool, with min_dy from the pool's price_oracle
         (manipulation-resistant) discounted by swap_fee_multiplier * pool fee.
    """
    token_set: DynArray[address, MAX_TOKENS] = staticcall self.fee_distributor.token_sets(
        staticcall self.fee_distributor.current_token_set())
    for lt_addr: address in token_set:
        lt: LT = LT(lt_addr)
        shares: uint256 = staticcall lt.balanceOf(self)
        if shares < self.dust_floor:
            continue
        pool: CryptoPool = staticcall lt.CRYPTOPOOL()
        asset: IERC20 = IERC20(staticcall pool.coins(1))
        asset_out: uint256 = extcall lt.withdraw(shares, 0, self)
        if asset_out == 0:
            continue
        # crvUSD out target from the EMA price, discounted by 1.5x the pool fee.
        # pool.fee() is scaled to FEE_DENOM (1e10); discount is rescaled to 1e18 and
        # capped at PRECISION so a large multiplier/fee floors min_dy at 0, not underflow.
        discount: uint256 = min(self.swap_fee_multiplier * (staticcall pool.fee()) // FEE_DENOM, PRECISION)
        min_dy: uint256 = asset_out * (staticcall pool.price_oracle()) // PRECISION * (PRECISION - discount) // PRECISION
        self._ensure_approval(asset, pool.address)
        extcall pool.exchange(1, 0, asset_out, min_dy, self)  # coin1 (asset) -> coin0 (crvUSD)


# --- controller --------------------------------------------------------------

@internal
@view
def _signals() -> (uint256, uint256, uint256):
    """
    @notice Compute the controller's manipulation-resistant inputs.
    @return pressure - max(0, summed net pressure) / H, 1e18.
    @return sink - sink-pool TVL / H, 1e18.
    @return H - sum of half-TVL over the pressure pools (crvUSD), the normalizer.
    """
    H: uint256 = 0
    net: int256 = 0
    for lt: address in self.pressure_lts:
        H += (staticcall self.net_pressure.pool_tvl_oracle(lt)) // 2
        net += staticcall self.net_pressure.net_pressure_oracle(lt)
    assert H > 0, "No pools"
    pressure: uint256 = 0
    if net > 0:
        pressure = convert(net, uint256) * PRECISION // H
    sink_abs: uint256 = (staticcall self.sink_pool.totalSupply()) * (staticcall self.sink_pool.get_virtual_price()) // PRECISION
    sink: uint256 = sink_abs * PRECISION // H
    return (pressure, sink, H)


@external
@nonreentrant
def trigger():
    """
    @notice Convert fees and (at most every min_interval) update the gauge rate.
            Permissionless; FeeSplitter calls it after forwarding the PID's share.
    """
    self._convert_fees()
    # Need strictly positive elapsed time (dt) for the integral/derivative; the
    # max(.,1) also makes min_interval=0 safe (avoids div-by-zero on same-block calls).
    if block.timestamp < self.last_ts + max(self.min_interval, 1):
        return  # too soon to step the controller; fees still converted above

    pressure: uint256 = 0
    sink: uint256 = 0
    H: uint256 = 0
    pressure, sink, H = self._signals()

    dt_years: int256 = convert((block.timestamp - self.last_ts) * PRECISION // SECONDS_PER_YEAR, int256)
    error: int256 = convert(pressure, int256) - convert(sink, int256)

    integral: int256 = self.integral + error * dt_years // convert(PRECISION, int256)
    integral = max(0, min(integral, self.max_integral))
    self.integral = integral

    d_pressure: int256 = 0
    if pressure > self.prev_pressure:
        d_pressure = convert(pressure - self.prev_pressure, int256) * convert(PRECISION, int256) // dt_years
    self.prev_pressure = pressure

    p18: int256 = convert(PRECISION, int256)
    target: int256 = (self.feedforward_gain * convert(pressure, int256) // p18
                      + self.kp * error // p18
                      + self.ki * integral // p18
                      + self.kd * d_pressure // p18)
    target = max(0, min(target, self.sink_cap))
    target_sink: uint256 = convert(target, uint256)

    offer_multiple: uint256 = self.dead_band + target_sink * PRECISION // self.sink_per_offer
    market_rate: uint256 = staticcall self.market_rate_getter.rate()
    bonus_apr: uint256 = 0
    if offer_multiple > PRECISION:
        bonus_apr = (offer_multiple - PRECISION) * market_rate // PRECISION

    # crvUSD/sec so stakers earn ~bonus_apr on the value they have staked.
    staked_value: uint256 = (staticcall self.gauge.totalAssets()) * (staticcall self.sink_pool.get_virtual_price()) // PRECISION
    rate: uint256 = bonus_apr * staked_value // PRECISION // SECONDS_PER_YEAR

    extcall self.gauge.set_reward_rate(rate)
    self.last_ts = block.timestamp
    log Trigger(pressure=pressure, sink=sink, bonus_apr=bonus_apr, rate=rate)


@external
@view
def preview_signals() -> (uint256, uint256, uint256):
    """
    @notice The controller's current inputs, for monitoring/tuning.
    @return pressure - max(0, summed net pressure) / H, 1e18.
    @return sink - sink-pool TVL / H, 1e18.
    @return H - sum of half-TVL over the pressure pools (crvUSD).
    """
    return self._signals()


# --- DAO control -------------------------------------------------------------

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
    log SetParams()


@external
def set_gauge(gauge: FastGauge, sink_pool: StableswapPool):
    """
    @notice Set the FastGauge + its sink pool, and approve crvUSD pulls.
    @dev DAO only. Grants the gauge an unlimited crvUSD allowance to pull rewards.
    @param gauge The FastGauge whose stream rate this controller sets.
    @param sink_pool The Curve stableswap pool whose TVL is the controller's sink.
    """
    ownable._check_owner()
    self.gauge = gauge
    self.sink_pool = sink_pool
    self._ensure_approval(CRVUSD, gauge.address)
    log SetParams()


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
    log SetParams()


@external
def set_gains(feedforward_gain: int256, kp: int256, ki: int256, kd: int256,
              max_integral: int256, sink_cap: int256, dead_band: uint256, sink_per_offer: uint256):
    """
    @notice Set the controller gains and clamps (all 1e18-scaled).
    @dev DAO only. Requires max_integral >= 0, sink_cap >= 0, sink_per_offer > 0.
    @param feedforward_gain Proportional gain on the raw pressure.
    @param kp Proportional gain on the coverage error (pressure - sink).
    @param ki Integral gain on the coverage error.
    @param kd Derivative gain on rising pressure.
    @param max_integral Clamp on the integral accumulator (anti-windup).
    @param sink_cap Clamp on the target sink.
    @param dead_band Offered APR multiple at zero target sink.
    @param sink_per_offer Target sink drawn per unit of offer above the dead band.
    """
    ownable._check_owner()
    assert max_integral >= 0 and sink_cap >= 0 and sink_per_offer > 0
    self.feedforward_gain = feedforward_gain
    self.kp = kp
    self.ki = ki
    self.kd = kd
    self.max_integral = max_integral
    self.sink_cap = sink_cap
    self.dead_band = dead_band
    self.sink_per_offer = sink_per_offer
    log SetParams()


@external
def set_execution_params(swap_fee_multiplier: uint256, min_interval: uint256, dust_floor: uint256):
    """
    @notice Set fee-conversion and cadence parameters.
    @dev DAO only.
    @param swap_fee_multiplier Slippage multiplier (1e18); min_dy = oracle*(1 -
           multiplier*pool_fee).
    @param min_interval Minimum seconds between controller steps.
    @param dust_floor LT balance below which fee conversion is skipped.
    """
    ownable._check_owner()
    self.swap_fee_multiplier = swap_fee_multiplier
    self.min_interval = min_interval
    self.dust_floor = dust_floor
    log SetParams()
