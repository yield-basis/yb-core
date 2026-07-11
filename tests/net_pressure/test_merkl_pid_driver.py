"""
Equivalence tests for MerklPIDDriver - the Merkl-side, *stateless* twin of PID.vy.

  * test_driver_matches_pid_vy: MerklPIDDriver.preview_target_apr reproduces the audited
    PID.vy controller EXACTLY (both Vyper, same truncating //) over a net-pressure path -
    same target/bonus APR and the same stepped (integral, prev_pressure, d_pressure) - when
    fed matched inputs, with PID's on-chain sink (gauge tvl_ema * sink vprice) handed to the
    driver as its Merkl-measured sink_tvl. No sdiv gap: two Vyper contracts, so it's exact.

  * test_python_reference_matches_driver: the off-chain Python reference Merkl runs
    (MerklPIDController.step, plain floor //) matches the on-chain view to <1e-9 relative.
    The ONLY divergence is floor-vs-truncate rounding (~1 wei per negative division, damped
    in the shipped APR), which is why this one is "almost", not "exactly", equal.

The Python reference (MerklPIDController) lives here so the thing Merkl mirrors is checked
in CI against both PID.vy and the on-chain view. Mocks come from conftest.py.
"""
import boa
import pytest
from collections import namedtuple


PRECISION = 10**18
SECONDS_PER_YEAR = 365 * 86400

# Field order matches the Vyper structs / set_gains(...) args, so these build straight from
# the boa return (`RawSignals(*driver.raw_signals())`) and splat back (`set_gains(*params)`).
RawSignals = namedtuple("RawSignals", "pressure half_tvl market_rate")
AprState = namedtuple("AprState", "target_apr integral prev_pressure d_pressure pressure sink")
Params = namedtuple("Params", "feedforward_gain kp ki kd max_integral sink_cap dead_band sink_per_offer d_filter_time")


class MerklPIDController:
    """Stateful off-chain controller Merkl runs: reads gains + signals from the driver,
    persists (integral, prev_pressure, d_pressure, last_ts), and each step returns the target
    APR. Uses plain floor // for readability (differs from the view's EVM truncation by ~1 wei
    per negative division). On the first call it starts from a clean slate, as the on-chain
    PID does on connect. Gains are re-read every step so a mid-flight set_gains is picked up."""

    def __init__(self, driver):
        self.driver = driver
        self.integral = 0
        self.prev_pressure = 0
        self.d_pressure = 0
        self.last_ts = None

    def read_params(self) -> Params:
        return Params(*(getattr(self.driver, n)() for n in Params._fields))

    def read_signals(self) -> RawSignals:
        return RawSignals(*self.driver.raw_signals())

    def step(self, sink_tvl, now_ts):
        p = self.read_params()                         # gains are DAO-tunable; re-read each step
        sig = self.read_signals()
        assert sig.half_tvl > 0, "No pools"
        if self.last_ts is None:                       # clean slate on connect
            self.last_ts, self.prev_pressure, self.integral, self.d_pressure = now_ts, sig.pressure, 0, 0
        dt = now_ts - self.last_ts

        sink = sink_tvl * PRECISION // sig.half_tvl
        dt_years = dt * PRECISION // SECONDS_PER_YEAR
        error = sig.pressure - sink

        # reads of self.* below are the *previous* state; the new state is written once at the end
        integral = self.integral + error * dt_years // PRECISION
        integral = max(0, min(integral, p.max_integral))

        # filtered derivative: d[k] = (Tf*d[k-1] + Δpressure)/(Tf + dt); Tf > 0 keeps dt==0 finite
        tf_years = p.d_filter_time * PRECISION // SECONDS_PER_YEAR
        dp = sig.pressure - self.prev_pressure
        d_pressure = (tf_years * self.d_pressure // PRECISION + dp) * PRECISION // (tf_years + dt_years)

        target = (p.feedforward_gain * sig.pressure // PRECISION
                  + p.kp * error // PRECISION
                  + p.ki * integral // PRECISION
                  + p.kd * max(0, d_pressure) // PRECISION)
        target = min(target, p.sink_cap)

        offer_multiple = max(p.dead_band + target * PRECISION // p.sink_per_offer, PRECISION)
        target_apr = (offer_multiple - PRECISION) * sig.market_rate // PRECISION if offer_multiple > PRECISION else 0

        self.integral, self.prev_pressure, self.d_pressure, self.last_ts = integral, sig.pressure, d_pressure, now_ts
        return target_apr


# --- fixtures ----------------------------------------------------------------

@pytest.fixture(scope="module")
def token():
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def accts():
    return [boa.env.generate_address() for _ in range(3)]


@pytest.fixture(scope="module")
def merkl_driver_deployer():
    return boa.load_partial("contracts/net_pressure/MerklPIDDriver.vy")


@pytest.fixture
def env(token, accts, np_mock, mr_mock, fd_mock, sink_mock, gauge_mock,
        factory_mock, agg_mock, splitter_mock, pid_deployer, merkl_driver_deployer):
    """PID.vy and MerklPIDDriver wired to the SAME oracle / market rate / factory / gains, so
    the only difference is PID's on-chain sink (gauge EMA) vs the driver's supplied sink_tvl."""
    admin = accts[0]
    crvusd = token.deploy("crvUSD", "crvUSD", 18)
    np = np_mock.deploy(0, 0)
    mr = mr_mock.deploy(35 * 10**15)                 # 3.5% market rate
    fd = fd_mock.deploy()                             # empty token set -> no fee conversion
    sink = sink_mock.deploy(10**24, 10**18)          # vprice 1.0
    gauge = gauge_mock.deploy(10**24)                # tvl_ema: staked LP in PID's gauge
    factory = factory_mock.deploy(agg_mock.deploy().address)
    lt = boa.env.generate_address()                  # the np mock ignores the lt arg

    pid = pid_deployer.deploy(crvusd.address, factory.address,
                              np.address, mr.address, fd.address, admin)
    # Connect PID: install a FeeSplitter-like fee_receiver whose pid() points back at it.
    factory.set_fee_receiver(splitter_mock.deploy(pid.address).address)
    with boa.env.prank(admin):
        pid.set_pressure_lts([lt])
        pid.set_gauge(gauge.address, sink.address)
        pid.set_execution_params(3 * 10**18 // 2, 10**12)

    driver = merkl_driver_deployer.deploy(crvusd.address, factory.address,
                                          np.address, mr.address, fd.address, admin)
    with boa.env.prank(admin):
        driver.set_pressure_lts([lt])
        driver.set_sink_pool(sink.address)           # informational; Merkl supplies the TVL
    return dict(pid=pid, driver=driver, np=np, mr=mr, sink=sink, gauge=gauge, admin=admin, crvusd=crvusd)


# net-pressure path: rising ramp that crosses the staked sink (sink_norm = 2.0, so net must
# exceed sink_abs = 1e24 to pay a bonus), a big jump over ONE second (the derivative spike),
# then a fall back below the sink to zero (the drain -> offer clamps to 1x -> APR 0). Same
# path the existing PID.vy reference test uses.
H = 5 * 10**23                                        # Σ half_tvl
TRAJ = [(1 * 10**24, 7200), (3 * 10**24, 7200), (6 * 10**24, 1), (1 * 10**24, 3600), (0, 7200)]


def test_driver_matches_pid_vy(env):
    pid, driver, np, sink, gauge = (env[k] for k in ("pid", "driver", "np", "sink", "gauge"))
    # Same gains by construction (both constructors default identically) - guard against drift.
    assert all(getattr(pid, n)() == getattr(driver, n)() for n in Params._fields)

    sink_abs = gauge.tvl_ema() * sink.get_virtual_price() // PRECISION   # PID's on-chain sink
    ctrl = MerklPIDController(driver)                                    # the off-chain reference
    now = 0
    np.set(0, H)
    pid.trigger()                                     # activate PID from a clean slate
    ctrl.step(sink_abs, now)                          # activate the reference at the same instant

    for net, dt in TRAJ:
        np.set(net, H)
        # PID's pre-step state = the inputs it will use this trigger (dt == the time we travel).
        i0, p0, d0 = pid.integral(), pid.prev_pressure(), pid.d_pressure()
        boa.env.time_travel(seconds=dt)
        now += dt
        pid.trigger()
        bonus_apr = [ev for ev in pid.get_logs() if hasattr(ev, "bonus_apr")][-1].bonus_apr
        ctx = f"net={net} dt={dt}"

        # (a) the stateless driver reproduces the ACTUAL PID.vy EXACTLY (both Vyper truncate):
        # same shipped APR and the same state PID.vy persisted on-chain.
        sol = AprState(*driver.preview_target_apr(sink_abs, i0, p0, d0, dt))
        assert sol.target_apr == bonus_apr, f"{ctx}: driver apr {sol.target_apr} != PID {bonus_apr}"
        assert sol.integral == pid.integral(), f"{ctx}: integral"
        assert sol.prev_pressure == pid.prev_pressure(), f"{ctx}: prev_pressure"
        assert sol.d_pressure == pid.d_pressure(), f"{ctx}: d_pressure"
        # inputs line up: same pressure (max(0,net)/H) and same sink (sink_abs/H)
        assert sol.pressure == max(0, net) * PRECISION // H, f"{ctx}: pressure"
        assert sol.sink == sink_abs * PRECISION // H, f"{ctx}: sink"

        # (b) the off-chain Python reference (floor //) tracks the ACTUAL PID.vy directly, to
        # sdiv rounding (a few wei; the 6h derivative filter is the only place it can diverge).
        ref_apr = ctrl.step(sink_abs, now)
        assert abs(ref_apr - bonus_apr) <= 64, f"{ctx}: python ref apr {ref_apr} vs PID {bonus_apr}"


# (dt, pressure fraction 1e18, sink fraction 1e18): net = frac*H//1e18 so pressure == frac,
# sink_tvl = frac*H//1e18 so sink == frac. Rise -> plateau -> sink overtakes -> drain to 0.
TRAJ_REF = [(0, 0, 0), (10800, 2 * 10**16, 0), (10800, 5 * 10**16, 1 * 10**16),
            (21600, 5 * 10**16, 3 * 10**16), (21600, 4 * 10**16, 6 * 10**16),
            (21600, 1 * 10**16, 9 * 10**16), (43200, 0, 9 * 10**16)]


def test_python_reference_matches_driver(env):
    """The off-chain Python reference (floor //) tracks the on-chain view (EVM truncate) to
    <1e-9 relative; the gap is only floor-vs-truncate rounding. Also exercises a mid-run
    set_gains: step() re-reads gains each step and the view reads them live, so they agree."""
    driver, np, admin = env["driver"], env["np"], env["admin"]
    ctrl = MerklPIDController(driver)
    now = 0
    worst_rel = 0.0
    for k, (dt, p_frac, s_frac) in enumerate(TRAJ_REF):
        np.set(p_frac * H // PRECISION, H)
        sink_tvl = s_frac * H // PRECISION
        now += dt
        if k == 3:                                    # DAO retunes mid-flight: double kd
            g = ctrl.read_params()
            with boa.env.prank(admin):
                driver.set_gains(*g._replace(kd=g.kd * 2))
        # capture the exact inputs step() will use (mirror its connect-time clean slate)
        if ctrl.last_ts is None:
            i_in, p_in, d_in, dt_used = 0, ctrl.read_signals().pressure, 0, 0
        else:
            i_in, p_in, d_in, dt_used = ctrl.integral, ctrl.prev_pressure, ctrl.d_pressure, now - ctrl.last_ts
        apr = ctrl.step(sink_tvl, now)
        sol = AprState(*driver.preview_target_apr(sink_tvl, i_in, p_in, d_in, dt_used))
        py = (apr, ctrl.integral, ctrl.prev_pressure, ctrl.d_pressure)
        ref = (sol.target_apr, sol.integral, sol.prev_pressure, sol.d_pressure)
        worst_rel = max(worst_rel, max(abs(a - b) / max(1, abs(a), abs(b)) for a, b in zip(py, ref)))
        assert apr == sol.target_apr, f"step {k}: shipped APR {apr} != view {sol.target_apr}"
    assert worst_rel < 1e-9, f"reference diverged {worst_rel:.2e} relative from the view"


ZERO = "0x0000000000000000000000000000000000000000"


def test_manager_role(env, accts):
    """The manager role may set_gains() (alongside the DAO); only the DAO appoints/clears it,
    and clearing it to 0x0 returns those controls to DAO-only."""
    driver, admin = env["driver"], env["admin"]
    manager, rando = accts[1], accts[2]
    g = [getattr(driver, n)() for n in Params._fields]     # current gains, to re-set unchanged

    # default: no manager -> only the DAO (owner) can set_gains
    assert driver.manager() == ZERO
    with boa.env.prank(rando):
        with boa.reverts():
            driver.set_gains(*g)
    with boa.env.prank(admin):
        driver.set_gains(*g)                               # owner ok

    # only the DAO can appoint a manager
    with boa.env.prank(rando):
        with boa.reverts():
            driver.set_manager(manager)
    with boa.env.prank(admin):
        driver.set_manager(manager)
    assert driver.manager() == manager

    # the manager can now set_gains; a random address still cannot
    with boa.env.prank(manager):
        driver.set_gains(*g)
    with boa.env.prank(rando):
        with boa.reverts():
            driver.set_gains(*g)

    # DAO clears the role (0x0) -> the former manager loses access, DAO still controls
    with boa.env.prank(admin):
        driver.set_manager(ZERO)
    assert driver.manager() == ZERO
    with boa.env.prank(manager):
        with boa.reverts():
            driver.set_gains(*g)
    with boa.env.prank(admin):
        driver.set_gains(*g)                               # owner still ok


# --- Merkl campaign create/override (Pull-on-Claim wrapper) -------------------

# Minimal stand-ins: WRAPPER_MOCK is the Pull-on-Claim wrapper (mint + ERC20 approve/transferFrom
# the driver touches); DC_MOCK is Merkl's DistributionCreator (typed createCampaign(P) so the
# driver's abi_encode must round-trip, pulls the wrapper from the caller, records the fields).
WRAPPER_MOCK = """
# pragma version 0.4.3
balanceOf: public(HashMap[address, uint256])
allowance: public(HashMap[address, HashMap[address, uint256]])
minted: public(uint256)
@external
def mint(amount: uint256):
    self.balanceOf[msg.sender] += amount
    self.minted += amount
@external
def approve(spender: address, amount: uint256) -> bool:
    self.allowance[msg.sender][spender] = amount
    return True
@external
def transferFrom(sender: address, receiver: address, amount: uint256) -> bool:
    self.allowance[sender][msg.sender] -= amount
    self.balanceOf[sender] -= amount
    self.balanceOf[receiver] += amount
    return True
"""

DC_MOCK = """
# pragma version 0.4.3
from ethereum.ercs import IERC20
struct P:
    campaign_id: bytes32
    creator: address
    reward_token: address
    amount: uint256
    campaign_type: uint32
    start_timestamp: uint32
    duration: uint32
    campaign_data: Bytes[4096]
signed: public(HashMap[address, bool])
counter: public(uint256)
last_creator: public(address)
last_reward_token: public(address)
last_amount: public(uint256)
last_type: public(uint32)
last_duration: public(uint32)
last_data: public(Bytes[4096])
last_override_id: public(bytes32)
@external
def acceptConditions():
    self.signed[msg.sender] = True
@external
def createCampaign(p: P) -> bytes32:
    assert self.signed[msg.sender], "not signed"
    extcall IERC20(p.reward_token).transferFrom(msg.sender, self, p.amount)
    self.last_creator = p.creator
    self.last_reward_token = p.reward_token
    self.last_amount = p.amount
    self.last_type = p.campaign_type
    self.last_duration = p.duration
    self.last_data = p.campaign_data
    self.counter += 1
    return convert(self.counter, bytes32)
@external
def overrideCampaign(campaign_id: bytes32, p: P):
    self.last_override_id = campaign_id
    self.last_data = p.campaign_data
    self.last_duration = p.duration
"""

MAX_UINT = 2**256 - 1


def test_merkl_campaign(env, accts):
    driver, admin, crvusd = env["driver"], env["admin"], env["crvusd"]
    manager, rando = accts[1], accts[2]
    dc = boa.loads(DC_MOCK)
    wrapper = boa.loads(WRAPPER_MOCK)

    # DAO installs Merkl + the wrapper -> both allowances granted (crvUSD->wrapper, wrapper->creator)
    with boa.env.prank(admin):
        driver.set_merkl(dc.address, wrapper.address)
    assert driver.merkl_creator() == dc.address
    assert driver.reward_wrapper() == wrapper.address
    assert crvusd.allowance(driver.address, wrapper.address) == MAX_UINT   # wrapper pulls crvUSD at claim
    assert wrapper.allowance(driver.address, dc.address) == MAX_UINT       # creator pulls the minted wrapper
    with boa.env.prank(rando):                                            # set_merkl is owner-only
        with boa.reverts():
            driver.set_merkl(dc.address, wrapper.address)

    # accept Merkl's terms (DAO or manager) so createCampaign's hasSigned passes
    with boa.env.prank(admin):
        driver.set_manager(manager)
    with boa.env.prank(manager):
        driver.accept_conditions()
    assert dc.signed(driver.address)

    # create: mints the wrapper cap, the DC pulls it, and the opaque campaign_data round-trips
    data = bytes(range(48))
    amount = 5000 * 10**18
    with boa.env.prank(manager):
        cid = driver.create_campaign(amount, 7, 0, 604800, data)
    assert wrapper.minted() == amount                     # driver minted the full cap
    assert wrapper.balanceOf(dc.address) == amount        # DC pulled it all (crvUSD stayed put)
    assert wrapper.balanceOf(driver.address) == 0
    assert dc.last_creator() == driver.address            # creator == this contract
    assert dc.last_reward_token() == wrapper.address      # reward token == the wrapper
    assert dc.last_amount() == amount
    assert dc.last_type() == 7
    assert dc.last_duration() == 604800
    assert dc.last_data() == data                         # abi_encode(struct) round-tripped the bytes
    assert cid == (1).to_bytes(32, "big")

    # override: new data/duration recorded (Merkl keeps amount/creator/token/id immutable)
    data2 = bytes(range(20, 40))
    with boa.env.prank(manager):
        driver.override_campaign(cid, 7, 0, 1209600, data2)
    assert dc.last_override_id() == cid
    assert dc.last_data() == data2
    assert dc.last_duration() == 1209600

    # gating: a random address can neither create nor override
    with boa.env.prank(rando):
        with boa.reverts():
            driver.create_campaign(amount, 7, 0, 604800, data)
        with boa.reverts():
            driver.override_campaign(cid, 7, 0, 604800, data2)


def test_set_merkl_revokes_old(env):
    """Re-installing a new creator/wrapper zeroes the previous pair's infinite allowances, so a
    replaced wrapper/creator keeps no pull on the reserve."""
    driver, admin, crvusd = env["driver"], env["admin"], env["crvusd"]
    dc1, wrapper1 = boa.loads(DC_MOCK), boa.loads(WRAPPER_MOCK)
    dc2, wrapper2 = boa.loads(DC_MOCK), boa.loads(WRAPPER_MOCK)

    with boa.env.prank(admin):
        driver.set_merkl(dc1.address, wrapper1.address)
    assert crvusd.allowance(driver.address, wrapper1.address) == MAX_UINT
    assert wrapper1.allowance(driver.address, dc1.address) == MAX_UINT

    with boa.env.prank(admin):
        driver.set_merkl(dc2.address, wrapper2.address)     # migrate
    # old pair fully revoked...
    assert crvusd.allowance(driver.address, wrapper1.address) == 0
    assert wrapper1.allowance(driver.address, dc1.address) == 0
    # ...new pair granted
    assert crvusd.allowance(driver.address, wrapper2.address) == MAX_UINT
    assert wrapper2.allowance(driver.address, dc2.address) == MAX_UINT

    # unset with 0x0: revokes the live pair and grants nothing to the zero address
    with boa.env.prank(admin):
        driver.set_merkl(ZERO, ZERO)
    assert driver.merkl_creator() == ZERO
    assert driver.reward_wrapper() == ZERO
    assert crvusd.allowance(driver.address, wrapper2.address) == 0
    assert wrapper2.allowance(driver.address, dc2.address) == 0
