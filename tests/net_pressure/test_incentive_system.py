"""
Unit tests for the net-pressure incentive system (FeeSplitter / PID / FastGauge /
MarketRateGetter), using small inline mocks so each contract is tested in isolation.
"""
import boa
import pytest


PRECISION = 10**18
SECONDS_PER_YEAR = 365 * 86400


# --- mocks -------------------------------------------------------------------

SUSDS_MOCK = """
# pragma version 0.4.3
ssr: public(uint256)
@deploy
def __init__(r: uint256):
    self.ssr = r
"""

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

PID_MOCK = """
# pragma version 0.4.3
triggered: public(uint256)
@external
def trigger():
    self.triggered += 1
"""

NP_MOCK = """
# pragma version 0.4.3
net: public(int256)
tvl: public(uint256)
@deploy
def __init__(n: int256, t: uint256):
    self.net = n
    self.tvl = t
@external
def set(n: int256, t: uint256):
    self.net = n
    self.tvl = t
@external
@view
def net_pressure_oracle(lt: address) -> int256:
    return self.net
@external
@view
def pool_tvl_oracle(lt: address) -> uint256:
    return self.tvl
"""

MR_MOCK = """
# pragma version 0.4.3
r: public(uint256)
@deploy
def __init__(x: uint256):
    self.r = x
@external
@view
def rate() -> uint256:
    return self.r
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

GAUGE_MOCK = """
# pragma version 0.4.3
last_rate: public(uint256)
ta: public(uint256)
@deploy
def __init__(a: uint256):
    self.ta = a
@external
def set_reward_rate(rate: uint256):
    self.last_rate = rate
@external
@view
def totalAssets() -> uint256:
    return self.ta
"""


@pytest.fixture(scope="module")
def token():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def accts():
    return [boa.env.generate_address() for _ in range(5)]


# --- MarketRateGetter --------------------------------------------------------

def test_market_rate_getter():
    ssr = 1000000001121484774769253326  # live-ish sUSDS value (~3.54% APR)
    susds = boa.loads(SUSDS_MOCK, ssr)
    getter = boa.load("contracts/net_pressure/MarketRateGetter.vy", susds.address)
    rate = getter.rate()
    # (ssr - RAY) * SECONDS_PER_YEAR / 1e9
    expected = (ssr - 10**27) * SECONDS_PER_YEAR // 10**9
    assert rate == expected
    assert 0.03 * 1e18 < rate < 0.05 * 1e18


def test_market_rate_getter_zero_rate_no_underflow():
    # ssr == RAY (0% rate) and ssr < RAY (degenerate) must return 0, not revert.
    susds = boa.loads(SUSDS_MOCK, 10**27)  # constructor requires ssr >= RAY
    getter = boa.load("contracts/net_pressure/MarketRateGetter.vy", susds.address)
    assert getter.rate() == 0                       # ssr == RAY
    susds.eval("self.ssr = 10**27 - 1")             # below RAY
    assert getter.rate() == 0                       # no underflow revert


# --- FastGauge ---------------------------------------------------------------

@pytest.fixture
def fg_setup(token, accts):
    admin, pid, u1, u2, _ = accts
    crvusd = token.deploy("crvUSD", "crvUSD", 18)
    lp = token.deploy("LP", "LP", 18)
    gauge = boa.load("contracts/net_pressure/FastGauge.vy", lp.address, crvusd.address, admin)
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


# --- FeeSplitter -------------------------------------------------------------

def test_feesplitter_split(token, accts):
    admin, _, _, _, _ = accts
    fd = boa.loads(FD_MOCK)
    pid = boa.loads(PID_MOCK)
    lt1 = token.deploy("LT1", "LT1", 18)
    lt2 = token.deploy("LT2", "LT2", 18)
    fd.set_tokens([lt1.address, lt2.address])

    fraction = PRECISION // 4  # 25% to PID
    fs = boa.load("contracts/net_pressure/FeeSplitter.vy", fd.address, pid.address, fraction, admin)

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


def test_feesplitter_recover(token, accts):
    admin, other = accts[0], accts[1]
    fd = boa.loads(FD_MOCK)
    fs = boa.load("contracts/net_pressure/FeeSplitter.vy", fd.address, accts[2], PRECISION // 2, admin)
    t = token.deploy("T", "T", 18)
    t._mint_for_testing(fs.address, 10**20)
    with boa.env.prank(other):
        with boa.reverts():
            fs.recover(t.address, 10**20, other)
    with boa.env.prank(admin):
        fs.recover(t.address, 10**20, admin)
    assert t.balanceOf(admin) == 10**20


def test_feesplitter_validates_distributor(token, accts):
    admin = accts[0]
    fd = boa.loads(FD_MOCK)
    fs = boa.load("contracts/net_pressure/FeeSplitter.vy", fd.address, accts[2], PRECISION // 2, admin)
    # A real FeeDistributor (responds to fill_epochs) is accepted.
    fd2 = boa.loads(FD_MOCK)
    with boa.env.prank(admin):
        fs.set_destinations(accts[2], fd2.address)
    assert fs.fee_distributor() == fd2.address
    # A bogus address without fill_epochs() is rejected by the checker.
    bogus = token.deploy("X", "X", 18)
    with boa.env.prank(admin):
        with boa.reverts():
            fs.set_destinations(accts[2], bogus.address)


# --- PID step vs reference ---------------------------------------------------

def _pid_reference(state, pressure, sink, market_rate, staked_value, dt_secs, g):
    dt_years = dt_secs * PRECISION // SECONDS_PER_YEAR
    error = pressure - sink
    integral = state["I"] + error * dt_years // PRECISION
    integral = max(0, min(integral, g["max_integral"]))
    state["I"] = integral
    d_pressure = 0
    if pressure > state["prevP"]:
        d_pressure = (pressure - state["prevP"]) * PRECISION // dt_years
    state["prevP"] = pressure
    target = (g["ff"] * pressure // PRECISION + g["kp"] * error // PRECISION
              + g["ki"] * integral // PRECISION + g["kd"] * d_pressure // PRECISION)
    target = max(0, min(target, g["sink_cap"]))
    offer = g["dead_band"] + target * PRECISION // g["sink_per_offer"]
    bonus = (offer - PRECISION) * market_rate // PRECISION if offer > PRECISION else 0
    rate = bonus * staked_value // PRECISION // SECONDS_PER_YEAR
    return rate


def test_pid_step_matches_reference(token, accts):
    admin = accts[0]
    crvusd = token.deploy("crvUSD", "crvUSD", 18)
    np = boa.loads(NP_MOCK, 0, 0)
    mr = boa.loads(MR_MOCK, 35 * 10**15)        # 3.5% market rate
    fd = boa.loads(FD_MOCK)                       # empty token set -> no conversion
    sink = boa.loads(SINK_MOCK, 10**24, 10**18)  # 1e24 LP, vprice 1.0
    gauge = boa.loads(GAUGE_MOCK, 5 * 10**23)    # totalAssets

    pid = boa.load("contracts/net_pressure/PID.vy", crvusd.address, np.address, mr.address, fd.address, admin)
    with boa.env.prank(admin):
        pid.set_pressure_lts([boa.env.generate_address()])
        pid.set_gauge(gauge.address, sink.address)
        pid.set_execution_params(3 * 10**18 // 2, 0, 10**12)  # min_interval=0

    g = dict(ff=pid.feedforward_gain(), kp=pid.kp(), ki=pid.ki(), kd=pid.kd(),
             max_integral=pid.max_integral(), sink_cap=pid.sink_cap(),
             dead_band=pid.dead_band(), sink_per_offer=pid.sink_per_offer())
    state = dict(I=0, prevP=0)
    vp = 10**18
    staked_value = 5 * 10**23 * vp // PRECISION

    # scripted pressure scenarios (absolute net crvUSD, pool tvl fixed)
    tvl = 10**24                      # H = tvl//2 = 5e23
    H = tvl // 2
    dt = 7200                         # 2-hour steps
    for net in [2 * 10**22, 5 * 10**22, 5 * 10**22, 1 * 10**22, 0]:
        np.set(net, tvl)
        boa.env.time_travel(seconds=dt)
        pressure = (max(0, net) * PRECISION) // H
        sink_abs = sink.totalSupply() * sink.get_virtual_price() // PRECISION
        sink_norm = sink_abs * PRECISION // H
        expected = _pid_reference(state, pressure, sink_norm, mr.rate(), staked_value, dt, g)
        pid.trigger()
        assert gauge.last_rate() == expected, f"net={net}: {gauge.last_rate()} != {expected}"
