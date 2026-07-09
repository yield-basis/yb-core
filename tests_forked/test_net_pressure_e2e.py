"""
End-to-end fork test of the net-pressure incentive system on live YB state.

Pinned to a block (E2E_BLOCK) found by scanning the last 3 days for the largest pending
LT admin fee across YB markets 7-10: at this block market 7 (yb-WBTC) has ~0.069 LT shares
pending (withdraw_admin_fees would mint them to the fee_receiver) and markets 8-10 have
none. The controller aggregates the net pressure of ALL four markets (~12-13% of half-TVL
here - markets 8/9 are imbalanced). A deliberately over-provisioned sink (far above the
~$2M live crvUSD/pyUSD pool, so the controller wants no more sink) makes the DEFAULT
controller gains produce a ZERO reward rate - the "nothing to incentivize" case. A
deliberately mis-tuned gain set (kp=0 so the controller ignores the existing coverage, plus
a raised dead band) instead produces a POSITIVE rate. Both are run as parameters of the
same test.

The FeeDistributor is the REAL one (contracts/dao/FeeDistributor.vy, live at the Factory's
current fee_receiver): the FeeSplitter/PID read its token set (markets 3-10) through its
actual element getter token_sets(set_id, i). Only market 7 has pending fees at this block.

Flow exercised by FeeSplitter.trigger():
  withdraw_admin_fees() on each LT in the real FeeDistributor's token set (mints fee shares
    to the FeeSplitter = Factory fee_receiver)
    -> split split_fraction to the PID, the rest back to the real FeeDistributor
    -> PID converts its LT shares to a crvUSD reserve (LT.withdraw + cryptopool swap)
    -> PID controller sets the FastGauge crvUSD/sec rate from the real net pressure
  then a staker of the crvUSD/pyUSD sink LP accrues and claims crvUSD from the gauge.
"""
import boa
import pytest
from tests_forked.networks import NETWORK

E2E_BLOCK = 25473385
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
SINK_LP = "0x625E92624Bc2D88619ACCc1788365A69767f6200"   # crvUSD/pyUSD stableswap == its LP
SUSDS = "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"      # Sky Savings Rate, for MarketRateGetter
MARKET_IDS = [7, 8, 9, 10]                                # aggregated for pressure + fee set
FEE_MARKET = 7                                            # the only 7-10 with pending fees here
DEPRECATED_MARKETS = [0, 1, 2]                            # long-deprecated; their fees are DAO-recoverable
FD_OWNER = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"  # FeeDistributor owner (can recover_token)
ZAP_SLIPPAGE = 3 * 10**18 // 2                            # 1.5x: enough slippage room to convert all three

SPLIT_FRACTION = 15 * 10**16                              # 15% of fees to the PID reserve (self-funds the spend)
# Staked sink, fabricated via boa.deal(adjust_supply=False) so the pool's virtual_price is
# left intact (bumping totalSupply would dilute get_virtual_price and mis-value the sink).
# Sized well above the AGGREGATE net pressure of all four markets (~$92M half-TVL, ~11.6%
# pressure - markets 8/9 are imbalanced here) so the controller wants NO more sink -> zero
# rate under the default gains, dominating even the first-trigger derivative kick. Far
# larger than the ~$2M live pool: a deliberately over-provisioned sink.
SINK_LP_AMOUNT = 60_000_000 * 10**18

# Contract-default gains (see PID.__init__). With the sink above covering market 7's small
# net pressure, the controller wants no sink -> offer floors at 1x -> rate 0.
DEFAULT_GAINS = dict(
    feedforward_gain=1_160_000_000_000_000_000, kp=50 * 10**18, ki=1988 * 10**18,
    kd=49_000_000_000_000_000, max_integral=2_930_000_000_000_000_000, sink_cap=22 * 10**18,
    dead_band=1_600_000_000_000_000_000, sink_per_offer=500_000_000_000_000_000,
    d_filter_time=6 * 3600)
# Deliberately mis-tuned: kp=0 makes the controller ignore how much sink already exists, and
# a dead band of 3x forces an offer above 1x regardless -> a positive rate is streamed.
WRONG_GAINS = {**DEFAULT_GAINS, "kp": 0, "dead_band": 3 * 10**18}

ERC20_ABI = """[
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
 {"name":"totalSupply","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address"},{"type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


@pytest.fixture(autouse=True)
def forked_env():
    # Own fork at E2E_BLOCK (the conftest forks at a different block); function-scoped so each
    # parametrization starts from the same intact pending fees.
    with boa.fork(NETWORK, block_identifier=E2E_BLOCK):
        yield


def _gains_tuple(g):
    return (g["feedforward_gain"], g["kp"], g["ki"], g["kd"], g["max_integral"],
            g["sink_cap"], g["dead_band"], g["sink_per_offer"], g["d_filter_time"])


def _deploy_system(fd_addr, pressure_lts, gains, owner):
    """Deploy YBNetPressure/MarketRateGetter/FastGauge/PID/FeeSplitter and wire them, with
    the given controller gains. `fd_addr` is the real FeeDistributor (fee/token-set source);
    `pressure_lts` is the DAO-selected set whose net pressure the controller aggregates."""
    oracle = boa.load("contracts/net_pressure/YBNetPressure.vy")
    mrate = boa.load("contracts/net_pressure/MarketRateGetter.vy", SUSDS)
    gauge = boa.load("contracts/net_pressure/FastGauge.vy", "crvUSD/pyUSD", "PYcrv",
                     SINK_LP, CRVUSD, owner)
    pid = boa.load("contracts/net_pressure/PID.vy", CRVUSD, FACTORY, oracle.address,
                   mrate.address, fd_addr, owner)
    fs = boa.load("contracts/net_pressure/FeeSplitter.vy", fd_addr, pid.address,
                  SPLIT_FRACTION, owner)
    with boa.env.prank(owner):
        pid.set_pressure_lts(pressure_lts)
        pid.set_gauge(gauge.address, SINK_LP)
        pid.set_gains(*_gains_tuple(gains))
        gauge.set_pid(pid.address)
    return oracle, mrate, gauge, pid, fs


@pytest.mark.parametrize("gains,expect_positive_rate",
                         [(DEFAULT_GAINS, False), (WRONG_GAINS, True)],
                         ids=["default_zero_rate", "wrong_positive_rate"])
def test_net_pressure_end_to_end(gains, expect_positive_rate):
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    lt_d = boa.load_partial("contracts/LT.vy")
    pressure_lts = [factory.markets(i).lt for i in MARKET_IDS]
    lt = lt_d.at(factory.markets(FEE_MARKET).lt)   # the only market with pending fees
    real_fd = factory.fee_receiver()               # the live FeeDistributor (contracts/dao)
    crvusd = boa.loads_abi(ERC20_ABI).at(CRVUSD)
    sink = boa.loads_abi(ERC20_ABI).at(SINK_LP)

    owner = boa.env.generate_address()
    staker = boa.env.generate_address()

    oracle, mrate, gauge, pid, fs = _deploy_system(real_fd, pressure_lts, gains, owner)

    # Point the Factory's fee_receiver at our FeeSplitter so withdraw_admin_fees() mints the
    # realized admin fees to it (impersonate the Factory admin, whatever contract it is).
    with boa.env.prank(factory.admin()):
        factory.set_fee_receiver(fs.address)

    # Stake a large sink of crvUSD/pyUSD LP and let the gauge's staked-EMA converge onto it.
    boa.deal(sink, staker, SINK_LP_AMOUNT, adjust_supply=False)
    with boa.env.prank(staker):
        sink.approve(gauge.address, 2**256 - 1)
        gauge.deposit(SINK_LP_AMOUNT, staker)
    boa.env.time_travel(seconds=20 * gauge.ema_time())     # tvl_ema -> SINK_LP_AMOUNT
    assert abs(gauge.tvl_ema() - SINK_LP_AMOUNT) < SINK_LP_AMOUNT // 100

    # Ground-truth the fee that WILL be realized this block: withdraw_admin_fees() mints to
    # the fee_receiver (= FeeSplitter). Measure it in an anchor, then roll back.
    with boa.env.anchor():
        fs_lt0 = lt.balanceOf(fs.address)
        lt.withdraw_admin_fees()
        realized = lt.balanceOf(fs.address) - fs_lt0
    assert realized > 0, "no admin fee pending at this block"
    to_pid = realized * SPLIT_FRACTION // 10**18
    to_fd = realized - to_pid

    fd_lt_before = lt.balanceOf(real_fd)
    pid_crvusd_before = crvusd.balanceOf(pid.address)

    # --- the whole flow -----------------------------------------------------
    fs.trigger()

    # 1) Fee split is exact: the real FeeDistributor got the remainder of market 7's fee, the
    #    PID converted its share, and the FeeSplitter is left empty.
    assert lt.balanceOf(real_fd) - fd_lt_before == to_fd
    assert lt.balanceOf(fs.address) == 0
    assert lt.balanceOf(pid.address) == 0, "PID did not fully convert its LT shares"

    # 2) The PID's share was converted into a crvUSD reserve.
    pid_reserve = crvusd.balanceOf(pid.address) - pid_crvusd_before
    assert pid_reserve > 0, "PID accumulated no crvUSD reserve"

    # 3) The controller set the gauge rate according to the gains.
    rate = gauge.reward_rate()
    if not expect_positive_rate:
        assert rate == 0, f"expected zero rate under default gains, got {rate}"
        return  # nothing streams; the fee-conversion half of the system is fully checked

    assert rate > 0, "expected a positive reward rate under the mis-tuned gains"

    # 4) Distribution flows: the staker accrues and claims crvUSD out of the PID reserve.
    reserve_before_claim = crvusd.balanceOf(pid.address)
    staker_before = crvusd.balanceOf(staker)
    boa.env.time_travel(seconds=3600)
    claimable = gauge.claimable_reward(staker)
    assert claimable > 0
    with boa.env.prank(staker):
        paid = gauge.claim(staker)
    assert paid > 0
    assert crvusd.balanceOf(staker) - staker_before == paid
    # The reward was pulled from the PID reserve (nothing minted out of thin air).
    assert crvusd.balanceOf(pid.address) < reserve_before_claim


WARMUP_SINK_LP = 400_000 * 10**18   # barely covers market 7 -> a positive settled rate once live


def test_controller_gated_off_until_connected():
    """The controller is OFF until the DAO installs our FeeSplitter as the Factory
    fee_receiver (PID._connected). Through the multi-day pre-connection window, permissionless
    pid.trigger() calls are no-ops: the integral never winds, the derivative stays 0 and the
    rate stays 0, and the pending admin fee is untouched. The instant the splitter is
    connected, the controller starts from a clean slate (no derivative kick, no windup) and
    the fee is claimed/split/converted."""
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    lt_d = boa.load_partial("contracts/LT.vy")
    lt = lt_d.at(factory.markets(FEE_MARKET).lt)
    real_fd = factory.fee_receiver()               # the live FeeDistributor (contracts/dao)
    crvusd = boa.loads_abi(ERC20_ABI).at(CRVUSD)
    sink = boa.loads_abi(ERC20_ABI).at(SINK_LP)
    owner = boa.env.generate_address()
    staker = boa.env.generate_address()

    # Pressure on a single market (7), default gains. The FeeSplitter is deployed but NOT yet
    # the fee_receiver, so the controller stays gated off until we connect below.
    oracle, mrate, gauge, pid, fs = _deploy_system(real_fd, [lt.address], DEFAULT_GAINS, owner)

    boa.deal(sink, staker, WARMUP_SINK_LP, adjust_supply=False)
    with boa.env.prank(staker):
        sink.approve(gauge.address, 2**256 - 1)
        gauge.deposit(WARMUP_SINK_LP, staker)
    boa.env.time_travel(seconds=20 * gauge.ema_time())

    # --- pre-connection window (~4 days): pid.trigger() is a no-op -----------
    for _ in range(9):
        pid.trigger()
        boa.env.time_travel(seconds=12 * 3600)
    assert not pid.active(), "controller must not run before the splitter is connected"
    assert pid.integral() == 0, "no windup allowed over the dead pre-connection window"
    assert pid.d_pressure() == 0
    assert gauge.reward_rate() == 0

    # --- connect the FeeSplitter and claim the (still-pending) fee ----------
    with boa.env.prank(factory.admin()):
        factory.set_fee_receiver(fs.address)

    fs_lt0 = lt.balanceOf(fs.address)
    with boa.env.anchor():
        lt.withdraw_admin_fees()
        realized = lt.balanceOf(fs.address) - fs_lt0
    assert realized > 0, "the pending fee must survive the pre-connection window intact"
    to_fd = realized - realized * SPLIT_FRACTION // 10**18

    fd_lt_before = lt.balanceOf(real_fd)
    pid_crvusd_before = crvusd.balanceOf(pid.address)
    fs.trigger()

    # Fee claimed and split as usual, converted into a crvUSD reserve.
    assert lt.balanceOf(real_fd) - fd_lt_before == to_fd
    assert lt.balanceOf(fs.address) == 0
    assert crvusd.balanceOf(pid.address) - pid_crvusd_before > 0

    # Clean start at connection: now active, integral still 0, no derivative kick, and the
    # rate is the settled value (positive for this barely-covering sink) - no cold-start bump.
    assert pid.active()
    assert pid.integral() == 0
    assert pid.d_pressure() == 0
    assert gauge.reward_rate() > 0


def test_pid_reserve_seeded_from_deprecated_fees_via_zap():
    """Seed the PID reserve from the long-deprecated markets' (0-2) fees, then stream them.

    Mirroring the real governance timeline: the system is deployed and sits through a ~4-day
    warmup during which permissionless pid.trigger() calls are gated no-ops (the FeeSplitter is
    not yet the fee_receiver). Right before the FeeSplitter is voted in, the DAO recovers the
    deprecated markets' LT fee shares from the live FeeDistributor, converts them to crvUSD with
    LTSwapZap, and sends the crvUSD to the PID as its starting reserve. Once the splitter is
    connected the controller runs from that zap-seeded reserve and a staker claims crvUSD out of
    it. (The clean-slate connection gate makes the warmup length immaterial to the result, but
    it is included to match how this would actually be rolled out.)"""
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    real_fd = factory.fee_receiver()               # the live FeeDistributor (contracts/dao)
    fd = boa.load_partial("contracts/dao/FeeDistributor.vy").at(real_fd)
    lt_d = boa.load_partial("contracts/LT.vy")
    pressure_lts = [factory.markets(i).lt for i in MARKET_IDS]
    deprecated_lts = [factory.markets(i).lt for i in DEPRECATED_MARKETS]
    crvusd = boa.loads_abi(ERC20_ABI).at(CRVUSD)
    sink = boa.loads_abi(ERC20_ABI).at(SINK_LP)

    dao = boa.env.generate_address()               # owns the PID + its reserve
    staker = boa.env.generate_address()

    # Mis-tuned gains (kp=0, wide dead band) so the controller streams a positive rate here.
    oracle, mrate, gauge, pid, fs = _deploy_system(real_fd, pressure_lts, WRONG_GAINS, dao)

    # A staker stakes the sink LP and the gauge's EMA converges onto it (needed for a rate).
    boa.deal(sink, staker, SINK_LP_AMOUNT, adjust_supply=False)
    with boa.env.prank(staker):
        sink.approve(gauge.address, 2**256 - 1)
        gauge.deposit(SINK_LP_AMOUNT, staker)
    boa.env.time_travel(seconds=20 * gauge.ema_time())

    # --- ~4-day warmup before the vote: pid.trigger() is a gated no-op ------
    for _ in range(8):                              # 8 * 12h = 4 days
        pid.trigger()
        boa.env.time_travel(seconds=12 * 3600)
    assert not pid.active(), "controller must not run before the splitter is connected"
    assert pid.integral() == 0 and pid.d_pressure() == 0
    assert gauge.reward_rate() == 0
    assert crvusd.balanceOf(pid.address) == 0, "no reserve before the DAO seeds it"

    # --- right before the vote: the DAO seeds the reserve via the zap -------
    zap = boa.load("contracts/utils/LTSwapZap.vy", CRVUSD, oracle.address, ZAP_SLIPPAGE, dao)
    with boa.env.prank(FD_OWNER):                   # recover the deprecated fee shares to the DAO
        for lt_addr in deprecated_lts:
            fd.recover_token(lt_addr, dao)          # token_balances==0 -> full balance
    recovered = [lt_d.at(lt_addr).balanceOf(dao) for lt_addr in deprecated_lts]
    assert all(s > 0 for s in recovered), "expected deprecated-market fee shares to recover"

    seed = 0
    with boa.env.prank(dao):
        for lt_addr in deprecated_lts:
            boa.loads_abi(ERC20_ABI).at(lt_addr).approve(zap.address, 2**256 - 1)
            seed += zap.convert(lt_addr)            # crvUSD -> the DAO
        crvusd.transfer(pid.address, seed)          # ...forwarded to the PID as its reserve
    assert seed > 0, "the zap realized no crvUSD from the deprecated fees"
    assert crvusd.balanceOf(pid.address) == seed, "PID reserve must be exactly the zap proceeds"
    print(f"\nseeded PID reserve from deprecated markets 0-2: {seed / 1e18:,.2f} crvUSD")

    # --- the FeeSplitter is voted in (connected) ---------------------------
    with boa.env.prank(factory.admin()):
        factory.set_fee_receiver(fs.address)

    # --- the controller runs from the zap-seeded reserve -------------------
    pid.trigger()                                   # not fs.trigger(): reserve stays the pure seed
    assert pid.active()
    assert gauge.reward_rate() > 0, "expected a positive reward rate under the mis-tuned gains"

    # A staker accrues and claims crvUSD straight out of the seeded reserve.
    reserve_before = crvusd.balanceOf(pid.address)
    staker_before = crvusd.balanceOf(staker)
    boa.env.time_travel(seconds=3600)
    assert gauge.claimable_reward(staker) > 0
    with boa.env.prank(staker):
        paid = gauge.claim(staker)
    assert paid > 0
    assert crvusd.balanceOf(staker) - staker_before == paid
    # The reward was pulled from the zap-seeded reserve (nothing else ever funded the PID); the
    # gauge draws >= paid (a dust remainder truncates into the per-token accounting).
    assert crvusd.balanceOf(pid.address) < reserve_before
    assert reserve_before - crvusd.balanceOf(pid.address) >= paid
