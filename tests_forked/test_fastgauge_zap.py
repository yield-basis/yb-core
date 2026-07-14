"""
Fork test of the FastGauge zap helpers against BOTH Curve stableswap pool generations:
  - newer Stableswap-NG (DynArray amounts ABI): crvUSD/pyUSD 0x625E...
  - older fixed-array (uint256[2] ABI):          crvUSD/USDC  0x4DEce...
The gauge auto-detects which ABI the staked LP-pool uses (POOL_IS_DYNARRAY) in __init__ and
routes add_liquidity/remove_liquidity through the matching typed interface. remove_liquidity_
one_coin shares one selector across both. Each variant zaps in (deposit coins -> stake LP ->
mint shares) and back out (one-coin and balanced), all in a single call.
"""
import boa
import pytest
from tests_forked.networks import NETWORK

CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
NG_POOL = "0x625E92624Bc2D88619ACCc1788365A69767f6200"    # crvUSD/pyUSD, DynArray ABI
OLD_POOL = "0x4DEcE678ceceb27446b35C672dC7d61F30bAD69E"   # crvUSD/USDC, fixed uint256[2] ABI

ERC20_ABI = """[
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


@pytest.fixture(autouse=True)
def forked_env():
    with boa.fork(NETWORK):
        yield


def _at(addr):
    return boa.loads_abi(ERC20_ABI).at(addr)


@pytest.mark.parametrize("pool,is_dyn", [(NG_POOL, True), (OLD_POOL, False)], ids=["ng_dynarray", "old_static"])
def test_fastgauge_zap_in_and_out(pool, is_dyn):
    owner = boa.env.generate_address()
    user = boa.env.generate_address()
    gauge = boa.load("contracts/net_pressure/FastGauge.vy", "z", "z", pool, CRVUSD, owner)

    # The gauge detected the pool and its amounts ABI at deploy time.
    assert gauge.ZAP_ENABLED()
    assert gauge.POOL_IS_DYNARRAY() == is_dyn
    coin0, coin1 = _at(gauge.COIN0()), _at(gauge.COIN1())
    lp_token = _at(pool)

    # A wrong-length amounts array is rejected (2-coin pools only).
    with boa.reverts("Bad amounts length"):
        gauge.add_liquidity([1], 0, user)

    # Fund the user with $100k of each coin (respecting each coin's decimals).
    a0 = 100_000 * 10 ** coin0.decimals()
    a1 = 100_000 * 10 ** coin1.decimals()
    boa.deal(coin0, user, a0, adjust_supply=False)
    boa.deal(coin1, user, a1, adjust_supply=False)

    # --- zap IN: deposit both coins into the pool and stake the LP in one call ---
    with boa.env.prank(user):
        coin0.approve(gauge.address, 2**256 - 1)
        coin1.approve(gauge.address, 2**256 - 1)
        lp = gauge.add_liquidity([a0, a1], 0, user)

    assert lp > 20 * 10**18, "expected a large LP mint (so partial exits stay above MIN supply)"
    assert gauge.balanceOf(user) == lp, "gauge shares must be 1:1 with staked LP"
    assert gauge.totalSupply() == lp
    assert lp_token.balanceOf(gauge.address) == lp, "staked LP must sit in the gauge"
    assert coin0.balanceOf(user) == 0 and coin1.balanceOf(user) == 0, "coins should be fully deposited"

    # --- zap OUT (single coin): unstake half as crvUSD (coin1) in one call ---
    crvusd = _at(CRVUSD)
    assert gauge.COIN1() == CRVUSD  # coin1 is crvUSD in both pools
    crv_before = crvusd.balanceOf(user)
    with boa.env.prank(user):
        dy = gauge.remove_liquidity_one_coin(lp // 2, 1, 0, user)
    assert dy > 0
    assert crvusd.balanceOf(user) - crv_before == dy, "crvUSD must go straight to the caller"
    assert gauge.balanceOf(user) == lp - lp // 2, "shares burned 1:1 with removed LP"
    assert lp_token.balanceOf(gauge.address) == lp - lp // 2

    # --- zap OUT (balanced): remove the rest as both coins in one call ---
    remaining = gauge.balanceOf(user)
    c0_before, c1_before = coin0.balanceOf(user), coin1.balanceOf(user)
    with boa.env.prank(user):
        out = gauge.remove_liquidity(remaining, [0, 0], user)
    assert out[0] > 0 and out[1] > 0
    assert coin0.balanceOf(user) - c0_before == out[0]
    assert coin1.balanceOf(user) - c1_before == out[1]
    assert gauge.balanceOf(user) == 0, "fully exited"
    assert gauge.totalSupply() == 0
    assert lp_token.balanceOf(gauge.address) == 0, "no LP left stranded in the gauge"


def test_fastgauge_zap_disabled_for_plain_lp():
    """A plain ERC20 LP (no coins()) leaves the zap disabled; the gauge is a pure staking gauge
    and the zap entrypoints revert."""
    owner = boa.env.generate_address()
    plain_lp = boa.load("contracts/testing/ERC20Mock.vy", "LP", "LP", 18)
    gauge = boa.load("contracts/net_pressure/FastGauge.vy", "z", "z", plain_lp.address, CRVUSD, owner)
    assert not gauge.ZAP_ENABLED()
    assert not gauge.POOL_IS_DYNARRAY()
    with boa.reverts("No zap: LP is not a pool"):
        gauge.add_liquidity([1, 1], 0, owner)
