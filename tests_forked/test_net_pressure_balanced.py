"""
Fork test of the net-pressure controller on a single BALANCED, unstressed pool: WBTC
(market 7) at a recent head block, where its net pressure is only ~1% of half-TVL.

It characterizes the cold-start behaviour the way the controller actually runs it: the very
first trigger sees pressure step 0 -> P, so the (filtered) derivative spikes and briefly
lifts the offer; over ~Tf (6h) that derivative decays and the offer settles to what the
feedforward/proportional terms alone sustain. With a sink that merely covers the small
pressure, that settled offer is a tiny residual (~1.3x market); with an over-provisioned
sink the rate is zero throughout. Either way the integral never winds up (no plant here,
but the sink already covers P, so the coverage error is <= 0) - i.e. a healthy pool costs
almost nothing.
"""
import boa
import pytest
from tests_forked.networks import NETWORK

BALANCED_BLOCK = 25483052        # recent head; WBTC net pressure ~1.05% of half-TVL
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
SINK_LP = "0x625E92624Bc2D88619ACCc1788365A69767f6200"
SUSDS = "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"
MARKET_ID = 7                    # yb-WBTC

ERC20_ABI = """[
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
 {"name":"totalSupply","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]"""


@pytest.fixture(autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier=BALANCED_BLOCK):
        yield


@pytest.mark.parametrize("sink_lp,zero_rate", [
    (300_000 * 10**18, False),     # barely covers P -> small residual bonus that decays
    (2_000_000 * 10**18, True),    # over-provisioned -> zero rate throughout
], ids=["barely_covered", "over_provisioned"])
def test_balanced_wbtc_cold_start_settles(sink_lp, zero_rate):
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    lt = factory.markets(MARKET_ID).lt
    real_fd = factory.fee_receiver()               # the live FeeDistributor (contracts/dao)
    sink = boa.loads_abi(ERC20_ABI).at(SINK_LP)
    owner = boa.env.generate_address()
    staker = boa.env.generate_address()

    oracle = boa.load("contracts/net_pressure/YBNetPressure.vy")
    mrate = boa.load("contracts/net_pressure/MarketRateGetter.vy", SUSDS)
    gauge = boa.load("contracts/net_pressure/FastGauge.vy", "WBTC", "wbtc", SINK_LP, CRVUSD, owner)
    pid = boa.load("contracts/net_pressure/PID.vy", CRVUSD, FACTORY, oracle.address,
                   mrate.address, real_fd, owner)
    fs = boa.load("contracts/net_pressure/FeeSplitter.vy", real_fd, pid.address,
                  10**18 // 2, owner)
    # Pressure on WBTC only; fees come from the real FeeDistributor's set (markets 3-10).
    with boa.env.prank(owner):
        pid.set_pressure_lts([lt])
        pid.set_gauge(gauge.address, SINK_LP)
        gauge.set_pid(pid.address)
    with boa.env.prank(factory.admin()):
        factory.set_fee_receiver(fs.address)

    # Stake the sink (vprice preserved) and converge the gauge's staked-EMA onto it.
    boa.deal(sink, staker, sink_lp, adjust_supply=False)
    with boa.env.prank(staker):
        sink.approve(gauge.address, 2**256 - 1)
        gauge.deposit(sink_lp, staker)
    boa.env.time_travel(seconds=20 * gauge.ema_time())

    # The pool is genuinely balanced: net pressure is a small % of half-TVL.
    sig = pid.preview_signals()
    assert 0 < sig.pressure < 2 * 10**16, f"expected a balanced pool, P={sig.pressure}"

    # Trigger the controller repeatedly, 6h apart, and watch the cold-start derivative decay.
    rates, derivs = [], []
    for _ in range(6):
        fs.trigger()
        rates.append(gauge.reward_rate())
        derivs.append(pid.d_pressure())
        boa.env.time_travel(seconds=6 * 3600)

    # The derivative kick (pressure stepped 0 -> P on the first trigger) decays away.
    assert derivs[-1] < derivs[0] // 2, f"derivative did not calm: {derivs}"
    # No integral windup: the sink already covers the small pressure (coverage error <= 0),
    # so the controller never leans on the integral - the offer is not driven to sink_cap.
    assert pid.integral() == 0

    if zero_rate:
        # Over-provisioned sink: the controller wants no sink, so rate is zero from the start
        # (the coverage term dominates even the cold-start derivative kick).
        assert all(r == 0 for r in rates), f"expected zero rate throughout, got {rates}"
    else:
        # Barely-covered: a positive rate that DECAYS from the cold-start transient toward the
        # small feedforward-sustained residual as the derivative calms.
        assert rates[0] > 0 and rates[-1] > 0
        assert rates[-1] < rates[0], f"cold-start rate should decay: {rates}"
