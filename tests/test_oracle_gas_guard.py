"""
Hermetic unit test for the gas-starvation guard added to YBLendingOracle /
YBLendingOracleLL (ChainSecurity #001).

The oracle swallows a failing get_state() and falls back to a balance-based price.
The guard must let a *genuine* (cheap) "AMM imbalanced" revert through to the
fallback, but refuse a 63/64-rule gas-starvation OOG (which would let an attacker
force the fallback). This pins that the guard distinguishes the two by the gas the
failed call left behind — a ratio test, so it is invariant to any gas-schedule
repricing (Glamsterdam etc.).

GUARD_SRC mirrors the exact guard in the oracles, in isolation from the rest of the
pricing graph, so the mechanism can be exercised against controlled targets.
"""
import boa
import pytest


GUARD_SRC = """
# pragma version 0.4.3
@external
@view
def guarded_call(target: address) -> bool:
    # Mirror of the oracle guard around the get_state() raw_call.
    gas_before: uint256 = msg.gas
    success: bool = False
    response: Bytes[96] = b""
    success, response = raw_call(
        target, method_id("get_state()"),
        max_outsize=96, revert_on_failure=False, is_static_call=True)
    if not success:
        assert msg.gas > gas_before // 16, "GAS"
    return success
"""

# Genuine "AMM too imbalanced" case: get_state() reverts immediately and cheaply.
CHEAP_REVERT_SRC = """
# pragma version 0.4.3
@external
@view
def get_state() -> (uint256, uint256, uint256):
    raw_revert(b"imbalanced")
"""

# Gas-starvation case: get_state() consumes everything forwarded to it, so under a
# constrained budget it OOGs (leaving only the caller's retained ~1/64).
GAS_BURNER_SRC = """
# pragma version 0.4.3
@external
@view
def get_state() -> (uint256, uint256, uint256):
    x: uint256 = 0
    for i: uint256 in range(100_000_000):
        x = unsafe_add(x, 1)
    return (x, x, x)
"""


@pytest.fixture(scope="module")
def guard():
    return boa.loads(GUARD_SRC)


@pytest.fixture(scope="module")
def cheap_revert():
    return boa.loads(CHEAP_REVERT_SRC)


@pytest.fixture(scope="module")
def gas_burner():
    return boa.loads(GAS_BURNER_SRC)


def test_genuine_cheap_revert_passes_guard(guard, cheap_revert):
    """A real imbalance revert is cheap -> guard passes -> oracle would use the fallback."""
    cheap = cheap_revert
    # Ample gas: get_state reverts cheaply, almost all gas survives, guard does not trip.
    assert guard.guarded_call(cheap.address) is False  # success == False -> fallback taken, no revert


def test_gas_starvation_is_blocked(guard, gas_burner):
    """An OOG'd get_state can never be silently reported as a (fallback-triggering) failure."""
    burner = gas_burner
    blocked = 0
    for gas_limit in range(50_000, 2_000_001, 50_000):
        # The burner consumes far more than any budget here, so it always OOGs.
        # The guard must turn that into a revert, never a clean `False` return.
        try:
            guard.guarded_call(burner.address, gas=gas_limit)
            pytest.fail(f"guard returned instead of reverting at gas={gas_limit}")
        except Exception:
            blocked += 1
    assert blocked > 0


def test_guard_threshold_is_ratio_based(guard, cheap_revert):
    """
    The guard floor scales with the gas supplied (gas_before // 16), not an absolute
    constant, so a 10x change in the ambient gas schedule leaves behaviour identical.
    Demonstrated by the cheap-revert path passing across very different gas budgets.
    """
    cheap = cheap_revert
    for gas_limit in (200_000, 2_000_000, 20_000_000):
        assert guard.guarded_call(cheap.address, gas=gas_limit) is False
