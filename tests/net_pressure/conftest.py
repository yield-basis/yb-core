"""Shared fixtures for the net-pressure incentive suite.

Compile-once deployers for the real contracts (YBNetPressure / MarketRateGetter /
FastGauge / PID / FeeSplitter) and the small inline mocks they are tested against,
so no test re-compiles or needlessly re-deploys these. The heavy YB market stack
(cryptopool / yb_lt / yb_amm / factory ...) comes from the top-level tests/conftest.py.
"""
import boa
import pytest


# --- mock contract sources ---------------------------------------------------

SUSDS_MOCK = """
# pragma version 0.4.3
ssr: public(uint256)
@deploy
def __init__(r: uint256):
    self.ssr = r
"""

# Mirrors the real FeeDistributor (contracts/dao/FeeDistributor.vy): token_sets is a
# DynArray[IERC20, MAX_TOKENS][N], so its only accessor is the element getter
# token_sets(set_id, i) -> token (2 args) - which is exactly what FeeSplitter/PID enumerate.
FD_MOCK = """
# pragma version 0.4.3
from ethereum.ercs import IERC20
MAX_TOKENS: constant(uint256) = 100
token_sets: public(DynArray[IERC20, MAX_TOKENS][16])
current_token_set: public(uint256)
filled: public(uint256)
@deploy
def __init__():
    self.current_token_set = 1
@external
def set_tokens(t: DynArray[IERC20, MAX_TOKENS]):
    self.token_sets[1] = t
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
struct PressureTvl:
    net_pressure: int256
    half_tvl: uint256
net: public(int256)
htvl: public(uint256)
@deploy
def __init__(n: int256, t: uint256):
    self.net = n
    self.htvl = t
@external
def set(n: int256, t: uint256):
    self.net = n
    self.htvl = t
@external
@view
def net_pressure_and_tvl(lt: address, agg_price: uint256) -> PressureTvl:
    return PressureTvl(net_pressure=self.net, half_tvl=self.htvl)
"""

AGG_MOCK = """
# pragma version 0.4.3
p: public(uint256)
@deploy
def __init__():
    self.p = 10**18
@external
@view
def price() -> uint256:
    return self.p
@external
def price_w() -> uint256:
    return self.p
"""

FACTORY_MOCK = """
# pragma version 0.4.3
agg: public(address)
fee_receiver: public(address)
@deploy
def __init__(a: address):
    self.agg = a
@external
def set_fee_receiver(fr: address):
    self.fee_receiver = fr
"""

# Stands in for the FeeSplitter in PID._connected(): the PID activates only when the
# Factory's fee_receiver is a contract whose pid() points back at it.
SPLITTER_MOCK = """
# pragma version 0.4.3
pid: public(address)
@deploy
def __init__(p: address):
    self.pid = p
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
tvl: public(uint256)
@deploy
def __init__(t: uint256):
    self.tvl = t
@external
def set_reward_rate(rate: uint256):
    self.last_rate = rate
@external
@view
def tvl_ema() -> uint256:
    return self.tvl
"""


# --- real-contract deployers (compile once, deploy per test) -----------------

@pytest.fixture(scope="session")
def net_pressure():
    # YBNetPressure is stateless (takes the LT as a call arg); safe to share read-only.
    return boa.load('contracts/net_pressure/YBNetPressure.vy')


@pytest.fixture(scope="session")
def mrate_getter_deployer():
    return boa.load_partial('contracts/net_pressure/MarketRateGetter.vy')


@pytest.fixture(scope="session")
def fastgauge_deployer():
    return boa.load_partial('contracts/net_pressure/FastGauge.vy')


@pytest.fixture(scope="session")
def pid_deployer():
    return boa.load_partial('contracts/net_pressure/PID.vy')


@pytest.fixture(scope="session")
def feesplitter_deployer():
    return boa.load_partial('contracts/net_pressure/FeeSplitter.vy')


# --- mock deployers (compile once, deploy per test) --------------------------

@pytest.fixture(scope="session")
def susds_mock():
    return boa.loads_partial(SUSDS_MOCK)


@pytest.fixture(scope="session")
def fd_mock():
    return boa.loads_partial(FD_MOCK)


@pytest.fixture(scope="session")
def pid_mock():
    return boa.loads_partial(PID_MOCK)


@pytest.fixture(scope="session")
def np_mock():
    return boa.loads_partial(NP_MOCK)


@pytest.fixture(scope="session")
def agg_mock():
    return boa.loads_partial(AGG_MOCK)


@pytest.fixture(scope="session")
def factory_mock():
    return boa.loads_partial(FACTORY_MOCK)


@pytest.fixture(scope="session")
def splitter_mock():
    return boa.loads_partial(SPLITTER_MOCK)


@pytest.fixture(scope="session")
def mr_mock():
    return boa.loads_partial(MR_MOCK)


@pytest.fixture(scope="session")
def sink_mock():
    return boa.loads_partial(SINK_MOCK)


@pytest.fixture(scope="session")
def gauge_mock():
    return boa.loads_partial(GAUGE_MOCK)
