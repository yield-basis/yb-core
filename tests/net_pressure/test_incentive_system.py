"""
Unit tests for the net-pressure incentive system (FeeSplitter / PID / FastGauge /
MarketRateGetter), using small inline mocks so each contract is tested in isolation.
"""
import boa
import pytest
from hypothesis import HealthCheck, given, settings
import hypothesis.strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine, rule, invariant, run_state_machine_as_test)


PRECISION = 10**18
SECONDS_PER_YEAR = 365 * 86400


# Mock sources and compile-once deployers (real contracts + mocks) live in conftest.py.


@pytest.fixture(scope="module")
def token():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def accts():
    return [boa.env.generate_address() for _ in range(5)]


# --- MarketRateGetter --------------------------------------------------------

def test_market_rate_getter(susds_mock, mrate_getter_deployer):
    ssr = 1000000001121484774769253326  # live-ish sUSDS value (~3.54% APR)
    susds = susds_mock.deploy(ssr)
    getter = mrate_getter_deployer.deploy(susds.address)
    rate = getter.rate()
    # (ssr - RAY) * SECONDS_PER_YEAR / 1e9
    expected = (ssr - 10**27) * SECONDS_PER_YEAR // 10**9
    assert rate == expected
    assert 0.03 * 1e18 < rate < 0.05 * 1e18


def test_market_rate_getter_zero_rate_no_underflow(susds_mock, mrate_getter_deployer):
    # ssr == RAY (0% rate) and ssr < RAY (degenerate) must return 0, not revert.
    susds = susds_mock.deploy(10**27)  # constructor requires ssr >= RAY
    getter = mrate_getter_deployer.deploy(susds.address)
    assert getter.rate() == 0                       # ssr == RAY
    susds.eval("self.ssr = 10**27 - 1")             # below RAY
    assert getter.rate() == 0                       # no underflow revert


# --- FastGauge ---------------------------------------------------------------

@pytest.fixture
def fg_setup(token, accts, fastgauge_deployer):
    admin, pid, u1, u2, _ = accts
    crvusd = token.deploy("crvUSD", "crvUSD", 18)
    lp = token.deploy("LP", "LP", 18)
    gauge = fastgauge_deployer.deploy("test", "t", lp.address, crvusd.address, admin)
    with boa.env.prank(admin):
        gauge.set_pid(pid)
    # fund the PID and approve the gauge to pull
    crvusd._mint_for_testing(pid, 10**24)
    with boa.env.prank(pid):
        crvusd.approve(gauge.address, 2**256 - 1)
    # give users LP and approve the gauge
    for u in (u1, u2):
        lp._mint_for_testing(u, 10**21)
        with boa.env.prank(u):
            lp.approve(gauge.address, 2**256 - 1)
    return dict(crvusd=crvusd, lp=lp, gauge=gauge, admin=admin, pid=pid, u1=u1, u2=u2)


def test_fastgauge_access_control(fg_setup):
    g, pid, u1 = fg_setup["gauge"], fg_setup["pid"], fg_setup["u1"]
    with boa.env.prank(u1):
        with boa.reverts("Only PID"):
            g.set_reward_rate(10**15)
    with boa.env.prank(pid):
        g.set_reward_rate(10**15)  # ok
    with boa.env.prank(u1):
        with boa.reverts():
            g.set_pid(u1)  # not owner


def test_fastgauge_min_total_supply(fg_setup):
    s = fg_setup
    g = s["gauge"]
    floor = g.MIN_TOTAL_SUPPLY()
    assert floor == 10 * 10**18
    # A deposit below the floor is rejected (can't bootstrap a tiny vault).
    with boa.env.prank(s["u1"]):
        with boa.reverts("Below min supply"):
            g.deposit(floor - 1, s["u1"])
    # At the floor it's accepted, and shares are 1:1 with the LP.
    with boa.env.prank(s["u1"]):
        shares = g.deposit(floor, s["u1"])
    assert shares == floor                     # 1e18 in -> 1e18 shares, no offset
    assert g.totalSupply() == floor
    # A partial withdrawal that would leave supply in (0, floor) is rejected.
    with boa.env.prank(s["u1"]):
        with boa.reverts("Below min supply"):
            g.withdraw(10**18, s["u1"], s["u1"])
    # Full exit to 0 is allowed.
    with boa.env.prank(s["u1"]):
        g.withdraw(floor, s["u1"], s["u1"])
    assert g.totalSupply() == 0


def test_fastgauge_available_from_pid_internal(fg_setup, fastgauge_deployer):
    s = fg_setup
    g, pid, crvusd, admin = s["gauge"], s["pid"], s["crvusd"], s["admin"]
    # min(PID balance, allowance); fixture funds PID and approves max -> balance.
    assert g.internal._available_from_pid() == crvusd.balanceOf(pid)
    # capped by allowance
    with boa.env.prank(pid):
        crvusd.approve(g.address, 10**18)
    assert g.internal._available_from_pid() == 10**18
    # capped by balance
    with boa.env.prank(pid):
        crvusd.approve(g.address, 2**256 - 1)
        crvusd.transfer(admin, crvusd.balanceOf(pid) - 5 * 10**17)
    assert g.internal._available_from_pid() == 5 * 10**17
    # no PID set -> 0
    g2 = fastgauge_deployer.deploy("crvUSD/pyUSD", "pyusd", s["lp"].address, crvusd.address, admin)
    assert g2.internal._available_from_pid() == 0


def test_fastgauge_single_staker_accrual(fg_setup):
    s = fg_setup
    g, crvusd = s["gauge"], s["crvusd"]
    rate = 10**15  # crvUSD/sec
    with boa.env.prank(s["pid"]):
        g.set_reward_rate(rate)
    with boa.env.prank(s["u1"]):
        g.deposit(10**20, s["u1"])
    boa.env.time_travel(seconds=1000)
    claimable = g.claimable_reward(s["u1"])
    assert abs(claimable - rate * 1000) <= rate  # ~rate*dt, within a second of dust
    with boa.env.prank(s["u1"]):
        g.claim(s["u1"])
    assert abs(crvusd.balanceOf(s["u1"]) - rate * 1000) <= rate


def test_fastgauge_two_stakers_split(fg_setup):
    s = fg_setup
    g = s["gauge"]
    rate = 10**15
    with boa.env.prank(s["pid"]):
        g.set_reward_rate(rate)
    with boa.env.prank(s["u1"]):
        g.deposit(10**20, s["u1"])
    with boa.env.prank(s["u2"]):
        g.deposit(10**20, s["u2"])  # equal stake
    boa.env.time_travel(seconds=1000)
    c1 = g.claimable_reward(s["u1"])
    c2 = g.claimable_reward(s["u2"])
    assert abs(c1 - c2) <= rate  # ~equal
    assert abs((c1 + c2) - rate * 1000) <= 5 * rate


# Matches FastGauge.MIN_TOTAL_SUPPLY: every stake >= the floor keeps every prefix sum
# above it, so deposits never trip the seed-the-market guard regardless of order.
MIN_STAKE = 10 * 10**18


@pytest.fixture
def gauge_env(token, accts, fastgauge_deployer):
    """A FastGauge with a deep, uncapped PID reserve and a pool of fresh stakers, so a
    property test can drive arbitrary stake splits without the pull ever being capped."""
    admin, pid = accts[0], accts[1]
    crvusd = token.deploy("crvUSD", "crvUSD", 18)
    lp = token.deploy("LP", "LP", 18)
    gauge = fastgauge_deployer.deploy("test", "t", lp.address, crvusd.address, admin)
    with boa.env.prank(admin):
        gauge.set_pid(pid)
    crvusd._mint_for_testing(pid, 10**30)  # deep reserve: the stream is never pull-capped
    with boa.env.prank(pid):
        crvusd.approve(gauge.address, 2**256 - 1)
    stakers = [boa.env.generate_address() for _ in range(6)]
    return dict(crvusd=crvusd, lp=lp, gauge=gauge, admin=admin, pid=pid, stakers=stakers)


@given(
    stakes=st.lists(st.integers(min_value=MIN_STAKE, max_value=10**26), min_size=1, max_size=6),
    rate=st.integers(min_value=1, max_value=10**18),
    dt=st.integers(min_value=1, max_value=30 * 86400),
)
@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_fastgauge_reward_split_proportional(gauge_env, stakes, rate, dt):
    """Property: for ANY split of stakes across ANY number of accounts, once all stakes
    are in and the stream runs for dt, each staker's claimable is exactly its share
    (stake_i / totalSupply) of what the gauge streamed. The rewards partition the stream
    with only integer-division dust, never over-paying.

    Deposits happen while rate == 0 (so no reward accrues during the staggered deposits
    regardless of per-tx timestamps); the rate is set once afterwards, giving a single
    clean accrual window of length dt over a fixed total supply.
    """
    g, lp = gauge_env["gauge"], gauge_env["lp"]
    pid = gauge_env["pid"]
    stakers = gauge_env["stakers"][:len(stakes)]

    for user, stake in zip(stakers, stakes):
        lp._mint_for_testing(user, stake)
        with boa.env.prank(user):
            lp.approve(g.address, 2**256 - 1)
            g.deposit(stake, user)          # rate still 0 -> integral stays 0

    supply = g.totalSupply()
    assert supply == sum(stakes)

    with boa.env.prank(pid):
        g.set_reward_rate(rate)             # opens the accrual window at "now"
    boa.env.time_travel(seconds=dt)

    # Deep reserve => the gauge pulls the full owed amount; reproduce the contract's exact
    # (double-floored) accounting: one global per-share integral, one per-user multiply.
    pulled = rate * dt
    integral = pulled * PRECISION // supply
    total = 0
    for user, stake in zip(stakers, stakes):
        expected = stake * integral // PRECISION
        assert g.claimable_reward(user) == expected, (stake, supply, rate, dt)
        total += expected

    # Conservation: the split never pays out more than was streamed, and the shortfall is
    # only integer-division dust (one per-user floor each, plus the single integral floor).
    assert total <= pulled
    assert pulled - total <= len(stakes) + supply // PRECISION + 1


def test_fastgauge_reward_split_claim_pays_out(gauge_env):
    """Each staker can actually claim exactly their proportional share (not just view it),
    and the gauge is left with only rounding dust."""
    g, lp, crvusd, pid = gauge_env["gauge"], gauge_env["lp"], gauge_env["crvusd"], gauge_env["pid"]
    stakes = [10 * 10**18, 30 * 10**18, 60 * 10**18]   # 1 : 3 : 6
    stakers = gauge_env["stakers"][:3]
    for user, stake in zip(stakers, stakes):
        lp._mint_for_testing(user, stake)
        with boa.env.prank(user):
            lp.approve(g.address, 2**256 - 1)
            g.deposit(stake, user)

    rate, dt = 10**15, 1000
    with boa.env.prank(pid):
        g.set_reward_rate(rate)
    boa.env.time_travel(seconds=dt)

    supply = sum(stakes)
    integral = rate * dt * PRECISION // supply
    for user, stake in zip(stakers, stakes):
        expected = stake * integral // PRECISION
        with boa.env.prank(user):
            g.claim(user)
        assert crvusd.balanceOf(user) == expected
    # Paid straight through: the gauge holds only the dust it couldn't divide evenly.
    assert crvusd.balanceOf(g.address) <= len(stakes) + supply // PRECISION + 1


def test_fastgauge_depletion_no_revert(fg_setup):
    s = fg_setup
    g, crvusd, pid = s["gauge"], s["crvusd"], s["pid"]
    # drain PID down to a tiny reserve
    bal = crvusd.balanceOf(pid)
    with boa.env.prank(pid):
        crvusd.transfer(s["admin"], bal - 10**15)  # leave 0.001 crvUSD
    rate = 10**18  # 1 crvUSD/sec -> will exhaust the reserve quickly
    with boa.env.prank(pid):
        g.set_reward_rate(rate)
    with boa.env.prank(s["u1"]):
        g.deposit(10**20, s["u1"])
    boa.env.time_travel(seconds=100000)  # far beyond what the reserve covers
    # No revert; claimable is capped by what the gauge could pull (<= reserve left).
    claimable = g.claimable_reward(s["u1"])
    assert claimable <= 10**15 + 10**6
    with boa.env.prank(s["u1"]):
        g.claim(s["u1"])  # must not revert
    # further time with empty reserve: still no revert, no growth
    boa.env.time_travel(seconds=1000)
    with boa.env.prank(s["u1"]):
        before = crvusd.balanceOf(s["u1"])
        g.claim(s["u1"])
        assert crvusd.balanceOf(s["u1"]) - before == 0


# --- FastGauge staked-LP EMA (manipulation resistance) -----------------------

def test_fastgauge_tvl_ema_flash_deposit_not_reflected(gauge_env):
    """A stake inflated and withdrawn within one block must not move tvl_ema(): the read
    inside the flash returns the pre-deposit value, and afterwards the EMA is unchanged."""
    g, lp = gauge_env["gauge"], gauge_env["lp"]
    attacker, honest = gauge_env["stakers"][0], gauge_env["stakers"][1]

    # A modest honest stake, then let the EMA settle onto it over time.
    lp._mint_for_testing(honest, 100 * 10**18)
    with boa.env.prank(honest):
        lp.approve(g.address, 2**256 - 1)
        g.deposit(100 * 10**18, honest)
    boa.env.time_travel(seconds=15 * g.ema_time())   # >> ema_time: EMA converges to the stake
    settled = g.tvl_ema()
    assert abs(settled - 100 * 10**18) < 10**16   # ~100 LP

    # Flash: deposit a huge stake, read within the SAME block, then withdraw it.
    huge = 10**24
    lp._mint_for_testing(attacker, huge)
    with boa.env.prank(attacker):
        lp.approve(g.address, 2**256 - 1)
        g.deposit(huge, attacker)
        during = g.tvl_ema()                  # read inside the flash
        g.withdraw(huge, attacker, attacker)
    # The in-flash read saw the pre-deposit value, not the inflated stake.
    assert during == settled
    # And the EMA is completely unmoved afterwards (same block, so no time weight).
    assert g.tvl_ema() == settled


def test_fastgauge_tvl_ema_converges_to_sustained_stake(gauge_env):
    """Held across time, the EMA converges toward the real staked amount; a partial
    reading after ~ema_time is between the old and new levels (monotone toward it)."""
    g, lp = gauge_env["gauge"], gauge_env["lp"]
    u = gauge_env["stakers"][0]

    lp._mint_for_testing(u, 50 * 10**18)
    with boa.env.prank(u):
        lp.approve(g.address, 2**256 - 1)
        g.deposit(50 * 10**18, u)
    # Right after the first deposit the EMA has not yet ramped from 0.
    assert g.tvl_ema() < 50 * 10**18
    boa.env.time_travel(seconds=g.ema_time())          # ~1 - 1/e of the way
    mid = g.tvl_ema()
    assert 0 < mid < 50 * 10**18
    boa.env.time_travel(seconds=20 * g.ema_time())     # long tail -> essentially there
    assert abs(g.tvl_ema() - 50 * 10**18) < 10**17


def test_fastgauge_set_ema_time_access_and_effect(gauge_env):
    g = gauge_env["gauge"]
    admin, outsider = gauge_env["admin"], gauge_env["stakers"][0]
    with boa.env.prank(outsider):
        with boa.reverts():
            g.set_ema_time(3600)
    with boa.env.prank(admin):
        with boa.reverts("ema_time"):
            g.set_ema_time(0)
        g.set_ema_time(7200)
    assert g.ema_time() == 7200


# --- FeeSplitter -------------------------------------------------------------

def test_feesplitter_split(token, accts, fd_mock, pid_mock, feesplitter_deployer):
    admin, _, _, _, _ = accts
    fd = fd_mock.deploy()
    pid = pid_mock.deploy()
    lt1 = token.deploy("LT1", "LT1", 18)
    lt2 = token.deploy("LT2", "LT2", 18)
    fd.set_tokens([lt1.address, lt2.address])

    fraction = PRECISION // 4  # 25% to PID
    fs = feesplitter_deployer.deploy(fd.address, pid.address, fraction, admin)

    # Simulate fees already minted to the splitter (admin-fee LT shares).
    lt1._mint_for_testing(fs.address, 10**20)
    lt2._mint_for_testing(fs.address, 4 * 10**20)

    fs.trigger()

    assert lt1.balanceOf(pid.address) == 10**20 // 4
    assert lt1.balanceOf(fd.address) == 10**20 - 10**20 // 4
    assert lt2.balanceOf(pid.address) == 4 * 10**20 // 4
    assert lt2.balanceOf(fd.address) == 4 * 10**20 - 4 * 10**20 // 4
    assert pid.triggered() == 1
    assert fd.filled() == 1


def test_feesplitter_recover(token, accts, fd_mock, feesplitter_deployer):
    admin, other = accts[0], accts[1]
    fd = fd_mock.deploy()
    fs = feesplitter_deployer.deploy(fd.address, accts[2], PRECISION // 2, admin)
    t = token.deploy("T", "T", 18)
    t._mint_for_testing(fs.address, 10**20)
    with boa.env.prank(other):
        with boa.reverts():
            fs.recover(t.address, 10**20, other)
    with boa.env.prank(admin):
        fs.recover(t.address, 10**20, admin)
    assert t.balanceOf(admin) == 10**20


def test_feesplitter_validates_distributor(token, accts, fd_mock, feesplitter_deployer):
    admin = accts[0]
    fd = fd_mock.deploy()
    fs = feesplitter_deployer.deploy(fd.address, accts[2], PRECISION // 2, admin)
    # A real FeeDistributor (responds to fill_epochs) is accepted.
    fd2 = fd_mock.deploy()
    with boa.env.prank(admin):
        fs.set_destinations(accts[2], fd2.address)
    assert fs.fee_distributor() == fd2.address
    # A bogus address without fill_epochs() is rejected by the checker.
    bogus = token.deploy("X", "X", 18)
    with boa.env.prank(admin):
        with boa.reverts():
            fs.set_destinations(accts[2], bogus.address)


# --- PID step vs reference ---------------------------------------------------

def _tdiv(a, b):
    # Truncate toward zero, matching Vyper int256 // (Python // floors); the filtered
    # derivative feeds back signed, so floor-vs-truncate would drift the reference.
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _pid_reference(state, pressure, sink, market_rate, staked_value, dt_secs, g):
    dt_years = dt_secs * PRECISION // SECONDS_PER_YEAR
    error = pressure - sink
    integral = state["I"] + _tdiv(error * dt_years, PRECISION)
    integral = max(0, min(integral, g["max_integral"]))
    state["I"] = integral
    # Filtered derivative: d[k] = (Tf*d[k-1] + Δpressure) / (Tf + dt).
    tf_years = g["d_filter_time"] * PRECISION // SECONDS_PER_YEAR
    dp = pressure - state["prevP"]
    d_pressure = _tdiv((_tdiv(tf_years * state["D"], PRECISION) + dp) * PRECISION, tf_years + dt_years)
    state["D"] = d_pressure
    state["prevP"] = pressure
    target = (_tdiv(g["ff"] * pressure, PRECISION) + _tdiv(g["kp"] * error, PRECISION)
              + _tdiv(g["ki"] * integral, PRECISION) + _tdiv(g["kd"] * max(0, d_pressure), PRECISION))
    target = min(target, g["sink_cap"])                          # signed; clamp only the top
    # Offer built signed and floored at 1x (matches PID.vy, and the signed-offer structure of
    # incentive_sim_linresp.py: xk = max(x_lo + raw, 1.0)). bonus is 0 when offer clamps to 1x.
    offer = max(g["dead_band"] + _tdiv(target * PRECISION, g["sink_per_offer"]), PRECISION)
    bonus = (offer - PRECISION) * market_rate // PRECISION if offer > PRECISION else 0
    rate = bonus * staked_value // PRECISION // SECONDS_PER_YEAR
    return rate


@pytest.fixture
def pid_env(token, accts, np_mock, mr_mock, fd_mock, sink_mock, gauge_mock,
            factory_mock, agg_mock, splitter_mock, pid_deployer):
    admin = accts[0]
    crvusd = token.deploy("crvUSD", "crvUSD", 18)
    np = np_mock.deploy(0, 0)
    mr = mr_mock.deploy(35 * 10**15)             # 3.5% market rate
    fd = fd_mock.deploy()                         # empty token set -> no conversion
    sink = sink_mock.deploy(10**24, 10**18)      # vprice 1.0
    gauge = gauge_mock.deploy(10**24)            # tvl_ema: staked LP in our gauge
    factory = factory_mock.deploy(agg_mock.deploy().address)
    pid = pid_deployer.deploy(crvusd.address, factory.address,
                              np.address, mr.address, fd.address, admin)
    # Connect: install a FeeSplitter-like fee_receiver whose pid() points back at the PID, so
    # PID._connected() is true and the controller runs (see the connection gate in trigger()).
    factory.set_fee_receiver(splitter_mock.deploy(pid.address).address)
    with boa.env.prank(admin):
        pid.set_pressure_lts([boa.env.generate_address()])
        pid.set_gauge(gauge.address, sink.address)
        pid.set_execution_params(3 * 10**18 // 2, 10**12)
    return dict(pid=pid, np=np, mr=mr, sink=sink, gauge=gauge, admin=admin)


def _gains(pid):
    return dict(ff=pid.feedforward_gain(), kp=pid.kp(), ki=pid.ki(), kd=pid.kd(),
                max_integral=pid.max_integral(), sink_cap=pid.sink_cap(),
                dead_band=pid.dead_band(), sink_per_offer=pid.sink_per_offer(),
                d_filter_time=pid.d_filter_time())


def test_pid_step_matches_reference(pid_env):
    pid, np, mr, sink, gauge = (pid_env[k] for k in ("pid", "np", "mr", "sink", "gauge"))
    g = _gains(pid)
    state = dict(I=0, prevP=0, D=0)
    # Sink and stream-scaling value both come from the gauge's manipulation-resistant
    # staked-LP EMA valued at the sink vprice (the same quantity now).
    sink_abs = gauge.tvl_ema() * sink.get_virtual_price() // PRECISION
    staked_value = sink_abs

    half_tvl = 5 * 10**23             # H = Σ half_tvl
    H = half_tvl
    # Activate the controller from a clean slate (net=0 -> prevP=0, I=0, D=0), matching the
    # reference's initial state, so the loop below compares like-for-like.
    np.set(0, H)
    pid.trigger()
    # (net, dt): a rising ramp that CROSSES the staked sink (sink_norm = 2.0 here, so net
    # must exceed sink_abs = 1e24 for the controller to want more sink and pay a bonus), a
    # big jump over a SINGLE second, then a fall back below the sink to zero pressure - so the
    # run exercises a positive bonus, the 1-block derivative spike, and the drain (offer
    # clamped to 1x -> rate 0). Both the rate and the derivative state are pinned.
    prev_p = 0
    for net, dt in [(1 * 10**24, 7200), (3 * 10**24, 7200), (6 * 10**24, 1),
                    (1 * 10**24, 3600), (0, 7200)]:
        np.set(net, half_tvl)
        boa.env.time_travel(seconds=dt)
        pressure = (max(0, net) * PRECISION) // H
        sink_norm = sink_abs * PRECISION // H
        expected = _pid_reference(state, pressure, sink_norm, mr.rate(), staked_value, dt, g)
        pid.trigger()
        assert pid.d_pressure() == state["D"], f"net={net} dt={dt}: derivative"
        assert gauge.last_rate() == expected, f"net={net} dt={dt}: {gauge.last_rate()} != {expected}"
        if dt == 1 and pressure != prev_p:
            # The one-second jump: the raw Δpressure/dt would be enormous; the filter keeps
            # the stored derivative orders of magnitude below it (no spike, no divergence).
            dt_years = dt * PRECISION // SECONDS_PER_YEAR
            raw = abs(pressure - prev_p) * PRECISION // dt_years
            assert abs(pid.d_pressure()) * 100 < raw
        prev_p = pressure


def test_pid_derivative_converges_to_ramp_slope(pid_env):
    """On a steady ramp (constant Δpressure each equal dt), the filtered derivative
    converges to the true slope Δpressure/dt - no bias from the smoothing."""
    pid, np = pid_env["pid"], pid_env["np"]
    H = 5 * 10**23
    dt = 3600
    step_net = 10**22                      # constant net increment per step
    np.set(0, H)
    pid.trigger()                          # activate the controller from a clean slate
    for i in range(1, 60):                 # >> Tf/dt (=6), so the filter fully converges
        np.set(i * step_net, H)
        boa.env.time_travel(seconds=dt)
        pid.trigger()

    dp_step = step_net * PRECISION // H     # pressure increment per step (1e18)
    dt_years = dt * PRECISION // SECONDS_PER_YEAR
    true_slope = dp_step * PRECISION // dt_years
    assert abs(pid.d_pressure() - true_slope) < true_slope // 100   # within 1%


def _pid_float_model(fs, pressure, sink, market, staked_value, dt_secs, g):
    """FLOAT dynamics model = the ground truth used for the research fits / sims in
    yb-research-scripts/rates:
      * PID step with the 6h Astrom-filtered derivative  -> sim_reserve_dfilter.py
        `register_pidf` and sim_pid_dfilter.py `astrom`;
      * signed offer floored at 1x (no perpetual dead-band baseline) -> the signed offer of
        incentive_sim_linresp.py, `xk = max(x_lo + raw, 1.0)`.
    The contract is 1e18 fixed point, so results are compared to this float model with a
    tolerance. `g` holds the (1e18-scaled) gains read from the deployed contract.
    Returns (rate crvUSD/s, target, offer, bonus, d_pressure) as floats. Mutates `fs`."""
    SPY = SECONDS_PER_YEAR
    dt = dt_secs / SPY
    ff, kp, ki, kd = g["ff"] / 1e18, g["kp"] / 1e18, g["ki"] / 1e18, g["kd"] / 1e18
    imax, scap = g["max_integral"] / 1e18, g["sink_cap"] / 1e18
    dead, spo = g["dead_band"] / 1e18, g["sink_per_offer"] / 1e18
    tf = g["d_filter_time"] / SPY
    P, S, m = pressure / 1e18, sink / 1e18, market / 1e18
    err = P - S
    I = min(max(fs["I"] + err * dt, 0.0), imax)                  # integral, clamped [0, imax]
    d = (tf * fs["D"] + (P - fs["prevP"])) / (tf + dt)           # Astrom filtered derivative
    fs["I"], fs["D"], fs["prevP"] = I, d, P
    target = ff * P + kp * err + ki * I + kd * max(0.0, d)
    target = min(target, scap)                                   # signed; clamp only the top
    offer = max(1.0, dead + target / spo)                       # signed offer, floored at 1x
    bonus = (offer - 1.0) * m if offer > 1.0 else 0.0           # bonus APR as a fraction
    rate = bonus * staked_value / SPY                           # crvUSD wei/s (staked_value in wei)
    return rate, target, offer, bonus, d


def test_pid_matches_dynamics_model(pid_env):
    """The deployed PID step reproduces the float dynamics model behind the research
    fits/sims (yb-research-scripts/rates) to fixed-point tolerance, across a realistic
    ramp / crash-spike / decay-to-zero net-pressure path - and pins the offer fix: the
    reward rate is EXACTLY 0 once the controller wants no sink (offer clamps to 1x), never
    the perpetual (dead_band-1)*market the pre-fix code streamed."""
    pid, np, mr, sink, gauge = (pid_env[k] for k in ("pid", "np", "mr", "sink", "gauge"))
    g = _gains(pid)
    staked_value = gauge.tvl_ema() * sink.get_virtual_price() // PRECISION
    market = mr.rate()
    H = 5 * 10**23                              # sink_abs = 1e24 -> sink_norm = 2.0
    fs = dict(I=0.0, D=0.0, prevP=0.0)
    np.set(0, H)
    pid.trigger()                               # activate the controller from a clean slate
    # net crosses the sink (net > 1e24 => pressure > sink => wants sink), spikes over 1s,
    # then falls to zero (drain). pressures: 2, 6, 12, 2, 0, 0, 0.
    path = [(1 * 10**24, 7200), (3 * 10**24, 7200), (6 * 10**24, 1),
            (1 * 10**24, 3600), (0, 7200), (0, 7200), (0, 30 * 86400)]
    saw_positive = saw_zero = False
    for net, dt in path:
        np.set(net, H)
        boa.env.time_travel(seconds=dt)
        pressure = (max(0, net) * PRECISION) // H
        sink_norm = staked_value * PRECISION // H
        rate_f, _tgt, _off, bonus_f, d_f = _pid_float_model(
            fs, pressure, sink_norm, market, staked_value, dt, g)
        pid.trigger()
        rate_c = gauge.last_rate()
        d_c = pid.d_pressure() / 1e18
        # filtered derivative and reward rate agree with the float model (fixed-point tol)
        assert abs(d_c - d_f) <= 1e-6 * abs(d_f) + 1e-9, f"net={net} dt={dt}: d {d_c} vs {d_f}"
        assert abs(rate_c - rate_f) <= 1e-6 * abs(rate_f) + 2.0, \
            f"net={net} dt={dt}: rate {rate_c} vs {rate_f}"
        if bonus_f == 0.0:                      # model wants no sink -> contract pays nothing
            assert rate_c == 0, f"net={net}: model bonus 0 but contract rate {rate_c}"
            saw_zero = True
        if rate_c > 0:
            saw_positive = True
    assert saw_positive and saw_zero            # both regimes were exercised


def test_pid_no_bonus_when_no_sink_wanted(pid_env):
    """Regression for the dead-band baseline bug: at zero net pressure with a staked sink,
    the controller wants no sink (target < 0), so bonus_apr and the reward rate must be
    EXACTLY 0 - not `(dead_band-1)*market` on the whole stake forever. Matches the model,
    which only pays when the controller wants sink (incentive_sim_pyusd/leakage: `if st>0`;
    incentive_sim_linresp: offer clamps to 1x)."""
    pid, np, gauge = (pid_env[k] for k in ("pid", "np", "gauge"))
    assert gauge.tvl_ema() > 0                  # there IS a staked sink, so a bonus -> rate>0
    np.set(0, 5 * 10**23)                       # zero net pressure
    boa.env.time_travel(seconds=7200)
    pid.trigger()
    assert gauge.last_rate() == 0              # ...yet the controller offers 1x -> zero rate
    assert pid.d_pressure() == 0


def test_set_gains_rejects_zero_filter_time(pid_env):
    """Tf == 0 is forbidden: it is the derivative denominator floor (Tf + dt), so with the
    same-block dt==0 step allowed, Tf==0 would divide by zero. set_gains must reject it and
    accept any positive value."""
    pid, admin = pid_env["pid"], pid_env["admin"]
    g = _gains(pid)
    args = (g["ff"], g["kp"], g["ki"], g["kd"], g["max_integral"], g["sink_cap"],
            g["dead_band"], g["sink_per_offer"])
    with boa.env.prank(admin):
        with boa.reverts():
            pid.set_gains(*args, 0)             # Tf == 0 rejected
        pid.set_gains(*args, 3600)             # any positive Tf accepted
    assert pid.d_filter_time() == 3600


def test_setters_reject_out_of_bounds(pid_env):
    """Every DAO-set param is magnitude-bounded so the controller's int256 math can't
    overflow-revert and brick trigger(). Values past the ceiling are rejected; the ceiling
    itself is accepted."""
    pid, admin = pid_env["pid"], pid_env["admin"]
    BIG = 10**24               # MAX_PARAM / MAX_PARAM_SIGNED
    MAX_TF = 10**9             # MAX_FILTER_TIME (seconds)
    g = _gains(pid)
    base = [g["ff"], g["kp"], g["ki"], g["kd"], g["max_integral"], g["sink_cap"],
            g["dead_band"], g["sink_per_offer"], g["d_filter_time"]]
    idx = {"ff": 0, "kp": 1, "ki": 2, "kd": 3, "max_integral": 4, "sink_cap": 5,
           "dead_band": 6, "sink_per_offer": 7, "d_filter_time": 8}

    def gains(**over):
        a = list(base)
        for k, v in over.items():
            a[idx[k]] = v
        return a

    with boa.env.prank(admin):
        # signed gains are bounded on BOTH sides
        for k in ("ff", "kp", "ki", "kd"):
            with boa.reverts():
                pid.set_gains(*gains(**{k: BIG + 1}))
            with boa.reverts():
                pid.set_gains(*gains(**{k: -(BIG + 1)}))
        # non-negative clamps / unsigned params: upper bound
        for k in ("max_integral", "sink_cap", "dead_band", "sink_per_offer"):
            with boa.reverts():
                pid.set_gains(*gains(**{k: BIG + 1}))
        with boa.reverts():
            pid.set_gains(*gains(d_filter_time=MAX_TF + 1))
        # the ceilings themselves are accepted (signed gains at +/- the bound)
        pid.set_gains(*gains(ff=BIG, kp=-BIG, ki=BIG, kd=-BIG, max_integral=BIG,
                             sink_cap=BIG, dead_band=BIG, sink_per_offer=BIG,
                             d_filter_time=MAX_TF))
    assert pid.feedforward_gain() == BIG
    assert pid.kp() == -BIG
    assert pid.d_filter_time() == MAX_TF

    # execution params: swap_fee_multiplier is bounded too (it multiplies pool.fee())
    with boa.env.prank(admin):
        with boa.reverts():
            pid.set_execution_params(BIG + 1, 10**12)
        pid.set_execution_params(BIG, 10**12)
    assert pid.swap_fee_multiplier() == BIG


def test_trigger_same_block_last_state_wins(pid_env):
    """Two trigger()s in one block both succeed - a same-block re-trigger has dt == 0, which
    is safe because Tf > 0 keeps the derivative denominator positive - and the rate follows
    the LAST state in the block, not the first (no min-interval / same-block throttle)."""
    pid, np, gauge = (pid_env[k] for k in ("pid", "np", "gauge"))
    H = 5 * 10**23
    # Baseline step with real elapsed time: strong net pressure -> a positive reward rate.
    np.set(6 * 10**24, H)
    boa.env.time_travel(seconds=7200)
    pid.trigger()
    assert gauge.last_rate() > 0
    # SAME block (no time travel): drop net pressure to zero and re-trigger. dt == 0 must not
    # revert, and the controller now wants no sink -> the rate follows the last state to 0,
    # rather than staying pinned at the first trigger's positive value.
    np.set(0, H)
    pid.trigger()
    assert gauge.last_rate() == 0


# --- FastGauge stateful invariants -------------------------------------------

class StatefulFastGauge(RuleBasedStateMachine):
    """Drives a FastGauge through arbitrary interleavings of deposits, withdrawals,
    transfers, claims, rate changes, PID refills/allowance changes and time travel,
    checking the whole time that:

      * LP is fully backed (gauge LP balance == totalSupply == Σ share balances);
      * crvUSD is conserved exactly (everything pulled from the PID is either sitting
        in the gauge or was claimed - nothing created or lost);
      * the gauge is always solvent for settled claims (its crvUSD balance covers every
        user's settled claimable), so a claim/withdraw can never hit "not enough";
      * nothing reverts along any valid path (min-supply-respecting withdrawals, claims,
        transfers, claims with an empty reserve, ...).

    Teardown opens the reserve fully, drains all claims, and asserts that of everything
    the gauge ever streamed, only bounded integer-division dust stays behind - i.e. with
    stakers present, (nearly) all rewards get distributed.
    """
    uid = st.integers(min_value=0, max_value=3)
    amount = st.integers(min_value=MIN_STAKE, max_value=10**24)
    frac = st.integers(min_value=0, max_value=100)
    a_rate = st.integers(min_value=0, max_value=10**18)
    a_dt = st.integers(min_value=1, max_value=30 * 86400)
    a_reserve = st.integers(min_value=0, max_value=10**24)
    a_allowance = st.integers(min_value=0, max_value=2**256 - 1)

    def __init__(self):
        super().__init__()
        self.crvusd = self.token.deploy("crvUSD", "crvUSD", 18)
        self.lp = self.token.deploy("LP", "LP", 18)
        self.admin = self.accts[0]
        self.pid = self.accts[1]
        self.users = [boa.env.generate_address() for _ in range(4)]
        self.gauge = self.fastgauge_deployer.deploy(
            "test", "t", self.lp.address, self.crvusd.address, self.admin)
        with boa.env.prank(self.admin):
            self.gauge.set_pid(self.pid)
        self.pid_funded = 10**21                       # modest reserve: depletes/caps sometimes
        self.crvusd._mint_for_testing(self.pid, self.pid_funded)
        with boa.env.prank(self.pid):
            self.crvusd.approve(self.gauge.address, 2**256 - 1)
        for u in self.users:
            with boa.env.prank(u):
                self.lp.approve(self.gauge.address, 2**256 - 1)
        self.total_claimed = 0
        self.dust_budget = 0
        self.max_supply = 0

    def _supply(self):
        return self.gauge.totalSupply()

    def _bump_dust(self):
        # Upper bound on the integer-division dust a single checkpoint can strand: the
        # global integral floor (< supply/1e18 + 1) plus a per-settled-user floor (<1 each,
        # at most 2 users touched per action).
        self.dust_budget += self._supply() // 10**18 + 4

    # --- rules ---------------------------------------------------------------

    @rule(uid=uid, amount=amount)
    def deposit(self, uid, amount):
        u = self.users[uid]
        self._bump_dust()
        self.lp._mint_for_testing(u, amount)
        with boa.env.prank(u):
            self.gauge.deposit(amount, u)

    @rule(uid=uid, frac=frac)
    def withdraw(self, uid, frac):
        u = self.users[uid]
        bal = self.gauge.balanceOf(u)
        if bal == 0:
            return
        supply = self._supply()
        full_ok = (supply - bal == 0) or (supply - bal >= MIN_STAKE)
        partial_max = min(bal - 1, supply - MIN_STAKE)   # w < bal and supply - w >= MIN
        if frac == 100 and full_ok:
            w = bal
        elif partial_max >= 1:
            w = 1 + (partial_max - 1) * frac // 100
        elif full_ok:
            w = bal
        else:
            return   # can't withdraw anything without dropping supply into (0, MIN)
        self._bump_dust()
        with boa.env.prank(u):
            self.gauge.withdraw(w, u, u)

    @rule(a=uid, b=uid, frac=frac)
    def transfer(self, a, b, frac):
        ua = self.users[a]
        bal = self.gauge.balanceOf(ua)
        if bal == 0:
            return
        self._bump_dust()
        with boa.env.prank(ua):
            self.gauge.transfer(self.users[b], bal * frac // 100)

    @rule(uid=uid)
    def claim(self, uid):
        u = self.users[uid]
        self._bump_dust()
        with boa.env.prank(u):
            self.total_claimed += self.gauge.claim(u)

    @rule(rate=a_rate)
    def set_rate(self, rate):
        self._bump_dust()
        with boa.env.prank(self.pid):
            self.gauge.set_reward_rate(rate)

    @rule(dt=a_dt)
    def time_travel(self, dt):
        boa.env.time_travel(seconds=dt)

    @rule(reserve=a_reserve)
    def refill_pid(self, reserve):
        # Only ever ADD to the PID (mint); pulls are the sole outflow, so crvUSD
        # conservation stays checkable purely from balances.
        self.crvusd._mint_for_testing(self.pid, reserve)
        self.pid_funded += reserve

    @rule(allowance=a_allowance)
    def set_allowance(self, allowance):
        # Capping the PID->gauge allowance throttles the pull (the "not enough" path)
        # without moving any crvUSD.
        with boa.env.prank(self.pid):
            self.crvusd.approve(self.gauge.address, allowance)

    # --- invariants ----------------------------------------------------------

    @invariant()
    def lp_fully_backed(self):
        supply = self.gauge.totalSupply()
        assert self.lp.balanceOf(self.gauge.address) == supply
        assert sum(self.gauge.balanceOf(u) for u in self.users) == supply

    @invariant()
    def crvusd_conserved(self):
        pulled = self.pid_funded - self.crvusd.balanceOf(self.pid)
        assert pulled == self.crvusd.balanceOf(self.gauge.address) + self.total_claimed

    @invariant()
    def solvent_for_settled(self):
        settled = sum(self.gauge.claimable(u) for u in self.users)
        assert self.crvusd.balanceOf(self.gauge.address) >= settled

    @invariant()
    def tvl_ema_bounded(self):
        # The staked-LP EMA is a convex blend of past recorded supplies, so it can never
        # exceed the historical peak stake - a flash spike above it is impossible.
        self.max_supply = max(self.max_supply, self.gauge.totalSupply())
        assert self.gauge.tvl_ema() <= self.max_supply

    # --- teardown ------------------------------------------------------------

    def teardown(self):
        # Open the taps fully and flush every settled claim.
        self.crvusd._mint_for_testing(self.pid, 10**27)
        self.pid_funded += 10**27
        with boa.env.prank(self.pid):
            self.crvusd.approve(self.gauge.address, 2**256 - 1)
        for _ in range(2):
            for u in self.users:
                self._bump_dust()
                with boa.env.prank(u):
                    self.total_claimed += self.gauge.claim(u)

        # Conservation still exact after the flush.
        pulled = self.pid_funded - self.crvusd.balanceOf(self.pid)
        assert pulled == self.crvusd.balanceOf(self.gauge.address) + self.total_claimed
        # Nearly all streamed rewards reached users: only bounded integer dust remains.
        assert self.crvusd.balanceOf(self.gauge.address) <= self.dust_budget
        super().teardown()


def test_stateful_fastgauge(token, accts, fastgauge_deployer):
    StatefulFastGauge.TestCase.settings = settings(
        max_examples=50, stateful_step_count=30, deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture])
    for k, v in locals().items():
        setattr(StatefulFastGauge, k, v)
    run_state_machine_as_test(StatefulFastGauge)
