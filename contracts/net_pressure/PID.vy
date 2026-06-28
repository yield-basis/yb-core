# @version 0.4.3
"""
@title PID (net-pressure incentive controller)
@author Yield Basis
@license Copyright (c) 2025
@notice Converts LT fees into a crvUSD reserve and runs a PID control loop on the
        aggregate crvUSD net pressure of a DAO-selected set of YB pools. The output
        is a crvUSD/second reward rate set on a FastGauge, which incentivizes
        deposits into a "sink" Curve stableswap pool that relieves the pressure.
@dev Methodology from yb-research-scripts/rates/REPORT_dynamic_incentives.md.
     All control math is in 1e18 fixed point; gains/params are DAO-settable
     storage (not constants). The reserve is simply this contract's crvUSD balance;
     the FastGauge pulls from it at checkpoint, so depletion just stops the stream.
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


@deploy
def __init__(crvusd: IERC20, net_pressure: NetPressureOracle, market_rate_getter: MarketRateGetter,
             fee_distributor: FeeDistributor, owner: address):
    ownable.__init__()
    ownable._transfer_ownership(owner)
    CRVUSD = crvusd
    self.net_pressure = net_pressure
    self.market_rate_getter = market_rate_getter
    self.fee_distributor = fee_distributor

    # Defaults from the report (§7/§9); DAO can retune.
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
def _convert_fees():
    """Convert any held LT fees into crvUSD: withdraw the asset from each LT then
    swap it to crvUSD in that LT's cryptopool, with min_dy derived from the pool's
    price_oracle (manipulation-resistant) minus swap_fee_multiplier * pool fee."""
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
        # pool.fee() is scaled to FEE_DENOM (1e10); discount is rescaled to 1e18.
        discount: uint256 = self.swap_fee_multiplier * (staticcall pool.fee()) // FEE_DENOM
        min_dy: uint256 = asset_out * (staticcall pool.price_oracle()) // PRECISION * (PRECISION - discount) // PRECISION
        assert extcall asset.approve(pool.address, asset_out, default_return_value=True)
        extcall pool.exchange(1, 0, asset_out, min_dy, self)  # coin1 (asset) -> coin0 (crvUSD)


# --- controller --------------------------------------------------------------

@internal
@view
def _signals() -> (uint256, uint256, uint256):
    """Return (pressure, sink, half_tvl_sum H), all manipulation-resistant."""
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
    """(pressure, sink, half_tvl_sum) — for monitoring/tuning."""
    return self._signals()


# --- DAO control -------------------------------------------------------------

@external
def recover(token: IERC20, amount: uint256, to: address):
    """@notice Sweep the crvUSD reserve (or any token) out, e.g. by DAO vote."""
    ownable._check_owner()
    assert extcall token.transfer(to, amount, default_return_value=True)
    log Recover(token=token.address, amount=amount)


@external
def set_pressure_lts(lts: DynArray[address, MAX_POOLS]):
    ownable._check_owner()
    self.pressure_lts = lts
    log SetParams()


@external
def set_gauge(gauge: FastGauge, sink_pool: StableswapPool):
    """@notice Set the FastGauge + its sink pool, and approve crvUSD pulls."""
    ownable._check_owner()
    self.gauge = gauge
    self.sink_pool = sink_pool
    assert extcall CRVUSD.approve(gauge.address, max_value(uint256), default_return_value=True)
    log SetParams()


@external
def set_sources(net_pressure: NetPressureOracle, market_rate_getter: MarketRateGetter,
                fee_distributor: FeeDistributor):
    ownable._check_owner()
    self.net_pressure = net_pressure
    self.market_rate_getter = market_rate_getter
    self.fee_distributor = fee_distributor
    log SetParams()


@external
def set_gains(feedforward_gain: int256, kp: int256, ki: int256, kd: int256,
              max_integral: int256, sink_cap: int256, dead_band: uint256, sink_per_offer: uint256):
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
    ownable._check_owner()
    self.swap_fee_multiplier = swap_fee_multiplier
    self.min_interval = min_interval
    self.dust_floor = dust_floor
    log SetParams()
