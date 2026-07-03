"""
Full-stack integration for the net-pressure incentive system on a real YB market
(LT + AMM + Curve cryptopool). Uses small mocks only for the FeeDistributor (the
real one needs VE/vesting wiring) and the sink stableswap pool valuation.

Flow exercised:
  admin deposits -> LT shares ("fees") -> FeeSplitter.trigger()
    -> split fraction to PID, rest to FeeDistributor(mock).fill_epochs()
    -> PID converts its LT shares to crvUSD (LT.withdraw + cryptopool swap)
    -> PID controller sets the FastGauge crvUSD/sec rate from real net pressure
  staker deposits sink LP into FastGauge -> accrues -> claims crvUSD.
"""
import boa


FD_MOCK = """
# pragma version 0.4.3
from ethereum.ercs import IERC20
MAX_TOKENS: constant(uint256) = 100
cset: public(uint256)
sets: HashMap[uint256, DynArray[IERC20, MAX_TOKENS]]
filled: public(uint256)
@deploy
def __init__():
    self.cset = 1
@external
def set_tokens(t: DynArray[IERC20, MAX_TOKENS]):
    self.sets[1] = t
@external
@view
def current_token_set() -> uint256:
    return self.cset
@external
@view
def token_sets(i: uint256) -> DynArray[IERC20, MAX_TOKENS]:
    return self.sets[i]
@external
def fill_epochs():
    self.filled += 1
"""

SUSDS_MOCK = """
# pragma version 0.4.3
ssr: public(uint256)
@deploy
def __init__(r: uint256):
    self.ssr = r
"""

SINK_MOCK = """
# pragma version 0.4.3
ts: public(uint256)
vp: public(uint256)
@deploy
def __init__(t: uint256, v: uint256):
    self.ts = t
    self.vp = v
@external
@view
def totalSupply() -> uint256:
    return self.ts
@external
@view
def get_virtual_price() -> uint256:
    return self.vp
"""


def test_full_stack(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, token_mock, factory,
):
    staker = accounts[4]

    # Deepen the pool and open a real YB position.
    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)

    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 5 * 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(5 * 10**18, p * 5 // 10**18 * 10**18, 0)
        yb_lt.set_rate(0)

    # Create positive net pressure: push the crypto price down, settle the EMA.
    dumper = accounts[3]
    collateral_token._mint_for_testing(dumper, 30 * 10**18)
    with boa.env.prank(dumper):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        for _ in range(4):
            cryptopool.exchange(1, 0, 5 * 10**18, 0)
    for _ in range(30):
        boa.env.time_travel(1200)

    # --- deploy the system -------------------------------------------------
    oracle = boa.load("contracts/net_pressure/YBNetPressure.vy")
    susds = boa.loads(SUSDS_MOCK, 1000000001121484774769253326)  # ~3.5% APR
    mrate = boa.load("contracts/net_pressure/MarketRateGetter.vy", susds.address)
    fd = boa.loads(FD_MOCK)
    fd.set_tokens([yb_lt.address])
    sink_lp = token_mock.deploy("sinkLP", "sLP", 18)
    sink_pool = boa.loads(SINK_MOCK, 10**21, 10**18)  # modest sink so error>0

    gauge = boa.load("contracts/net_pressure/FastGauge.vy", sink_lp.address, stablecoin.address, admin)
    pid = boa.load("contracts/net_pressure/PID.vy", stablecoin.address, factory.address,
                   oracle.address, mrate.address, fd.address, admin)
    fraction = 10**18 // 2  # 50% to PID
    fs = boa.load("contracts/net_pressure/FeeSplitter.vy", fd.address, pid.address, fraction, admin)

    with boa.env.prank(admin):
        pid.set_pressure_lts([yb_lt.address])
        pid.set_gauge(gauge.address, sink_pool.address)
        pid.set_execution_params(3 * 10**18 // 2, 0, 10**12)  # min_interval=0
        gauge.set_pid(pid.address)

    net = oracle.net_pressure_oracle(yb_lt.address)
    assert net > 0, f"expected positive net pressure, got {net}"

    # Simulate fees arriving at the splitter: a small slice of LT shares (fees are
    # tiny vs pool depth, so the conversion swap has negligible price impact).
    lt_fee = yb_lt.balanceOf(admin) // 1000
    with boa.env.prank(admin):
        yb_lt.transfer(fs.address, lt_fee)

    # A staker stakes sink LP into the gauge (so staked_value > 0 -> rate > 0).
    sink_lp._mint_for_testing(staker, 10**21)
    with boa.env.prank(staker):
        sink_lp.approve(gauge.address, 2**256 - 1)
        gauge.deposit(10**21, staker)

    pid_crvusd_before = stablecoin.balanceOf(pid.address)
    fd_lt_before = yb_lt.balanceOf(fd.address)

    # --- the trigger --------------------------------------------------------
    boa.env.time_travel(seconds=7200)  # let dt elapse since PID deploy
    fs.trigger()

    # Split: half the LT fee went to the FeeDistributor, half to the PID (converted).
    assert yb_lt.balanceOf(fd.address) - fd_lt_before == lt_fee - lt_fee // 2
    assert fd.filled() == 1
    # PID converted its LT shares into a crvUSD reserve.
    pid_reserve = stablecoin.balanceOf(pid.address) - pid_crvusd_before
    assert pid_reserve > 0, "PID did not accumulate a crvUSD reserve"
    assert yb_lt.balanceOf(pid.address) == 0, "PID still holds unconverted LT"

    # Controller set a positive stream rate from the real net pressure.
    rate = gauge.reward_rate()
    assert rate > 0, "expected a positive reward rate under positive net pressure"

    # --- staker earns and claims crvUSD ------------------------------------
    boa.env.time_travel(seconds=3600)
    claimable = gauge.claimable_reward(staker)
    assert claimable > 0
    with boa.env.prank(staker):
        gauge.claim(staker)
    assert stablecoin.balanceOf(staker) > 0
    # The reward came out of the PID reserve.
    assert stablecoin.balanceOf(pid.address) < pid_crvusd_before + pid_reserve

    # --- a second trigger (a new tx) -------------------------------------------
    # yb_lt is in BOTH the fee set and pressure_lts, so trigger() caches its
    # net_pressure_and_tvl in transient storage during conversion and reuses it for the
    # controller (one lp_oracle_2 solve). The contract clears transient per tx; boa does
    # not between calls, so emulate a fresh tx here.
    boa.env.evm.vm.state.clear_transient_storage()
    lt_fee2 = yb_lt.balanceOf(admin) // 1000
    with boa.env.prank(admin):
        yb_lt.transfer(fs.address, lt_fee2)
    reserve_before2 = stablecoin.balanceOf(pid.address)
    boa.env.time_travel(seconds=7200)
    fs.trigger()
    assert fd.filled() == 2
    assert stablecoin.balanceOf(pid.address) > reserve_before2  # converted again
    assert gauge.reward_rate() > 0                              # rate refreshed
