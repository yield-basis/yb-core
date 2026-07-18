"""
Historical spot-checks of YBLendingOracleLL against an existing market (id 4, ybcbBTC) - the LL
has no other forked coverage, and the synthetic unit-test pool never reaches the long real
regions where the cryptopool's price_oracle diverges from price_scale.

Rather than sweep the whole lifetime, we hand-pick blocks from a one-off divergence profile of
market 4 (fork every ~10k blocks, read price_oracle/price_scale): mostly the high po-vs-ps
stress moments (up to ~6%), plus several calm points (<0.1%). At each, fork the node, deploy a
fresh LL + the reference YBLendingOracle, and check:

  1. exact twin    - LL.price() == YBLendingOracle.price_in_asset(lt) to rounding. A fresh
                     (unseeded) LL prices off the raw fundamental * live shift, i.e. the
                     resistant reference price by construction; this pins the decomposition
                     against real (pool, AMM, agg) states incl. the deeply divergent ones.
  2. vs redemption - LL.price() within 1% of lt.preview_withdraw(1e18) (the redemption
                     coefficient). The oracle marks at price_oracle while redemption is the live
                     withdraw, so they part in the divergent regions but stay close.

    uv run pytest -vv tests_forked/test_lending_oracle_ll_historical.py
"""
import boa
import pytest
from tests_forked.networks import NETWORK

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
ZERO = "0x" + "00" * 20
MARKET_ID = 4
EMA_TIME = 866

TWIN_TOL = 10**-7     # LL vs reference resistant path: a few wei of rounding (observed ~4e-18)
REDEEM_TOL = 10**-2   # LL vs preview_withdraw redemption: ~0.2% even at 5% divergence, 1% is safe

# (block, approx po-vs-ps divergence %) curated from a market-4 lifetime scan: high-divergence
# stress first, then mid, then calm. The divergence is deterministic per block; the test
# re-measures it and sanity-checks it against the annotation.
SAMPLES = [
    (23921000, 6.10), (23938500, 5.06), (23855000, 5.03), (23849000, 4.96),
    (23936000, 4.92), (23933500, 4.88), (23843896, 3.35), (23926000, 3.31),
    (23823956, 3.04), (23943593, 2.99), (24043290, 2.39), (23893744, 2.35),
    (23913684, 1.59), (24023351, 1.52), (24123048, 1.38), (23993442, 1.38),
    (23953563, 1.00), (24083169, 1.00), (24013381, 0.75), (24103109, 0.70),
    (24322443, 0.47), (23863835, 0.07), (24222745, 0.04), (23833926, 0.03),
    (24252654, 0.03), (24302503, 0.02), (24093139, 0.01),
]

ERC20_ABI = """[
    {"name":"decimals","outputs":[{"type":"uint8"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    """Override the conftest's single-block autouse fork: this module opens its own fork per
    sampled block, so it must NOT run inside an outer fork (nested forks + dirty state)."""
    yield


# Compile each contract ONCE (module scope). Compilation is env-independent, so these deployers
# are reused across every per-block fork - the test body only does .at()/.deploy(), never a
# recompile. (Compiling in the parametrized loop is what made an earlier version slow.)
@pytest.fixture(scope="module")
def deployers():
    return {
        "factory": boa.load_partial("contracts/Factory.vy"),
        "lt": boa.load_partial("contracts/LT.vy"),
        "pool": boa.load_partial("contracts/twocrypto_pool/contracts/main/Twocrypto.vy"),
        "proxy": boa.load_partial("contracts/utils/YBPriceProxy.vy"),
        "ref": boa.load_partial("contracts/utils/YBLendingOracle.vy"),
        "ll": boa.load_partial("contracts/utils/YBLendingOracleLL.vy"),
        "erc20": boa.loads_abi(ERC20_ABI),
    }


def _rel(a, b):
    return abs(int(a) - int(b)) / max(int(a), int(b), 1)


@pytest.mark.parametrize("block,approx_div_pct", SAMPLES,
                         ids=[f"blk{b}_div{d}" for b, d in SAMPLES])
def test_ll_matches_reference_and_redemption(deployers, block, approx_div_pct):
    with boa.fork(NETWORK, block_identifier=block, allow_dirty=True):
        factory = deployers["factory"].at(FACTORY)
        market = factory.markets(MARKET_ID)
        lt_addr = market.lt
        assert lt_addr != ZERO, f"market {MARKET_ID} absent at block {block}"

        lt = deployers["lt"].at(lt_addr)
        assert lt.totalSupply() > 0, f"no position at block {block}"
        pool = deployers["pool"].at(market.cryptopool)
        decimals = deployers["erc20"].at(market.asset_token).decimals()

        po, ps = pool.price_oracle(), pool.price_scale()
        div = abs(po - ps) / ps
        # Data-integrity: the block is really at the intended divergence regime (deterministic).
        assert abs(div * 100 - approx_div_pct) < max(0.5, approx_div_pct * 0.3), \
            f"block {block} divergence {div * 100:.3f}% != annotated {approx_div_pct}%"

        # Reference (resistant) + a fresh unseeded LL (raw fundamental * live shift).
        proxy = deployers["proxy"].deploy()
        ref = deployers["ref"].deploy(FACTORY, proxy.address)
        ll = deployers["ll"].deploy()
        ll.initialize(lt_addr, False, EMA_TIME, FACTORY)

        ll_price = ll.price()
        ref_price = ref.price_in_asset(lt_addr)
        redeem = lt.preview_withdraw(10**18) * 10 ** (18 - decimals)

        twin = _rel(ll_price, ref_price)
        redeem_diff = _rel(ll_price, redeem)
        print(f"\nblk {block} div {div * 100:.3f}%: LL {ll_price / 1e18:.8f}  "
              f"ref twin {twin:.2e}  redemption {redeem_diff:.4%}")

        assert twin <= TWIN_TOL, \
            f"blk {block}: LL {ll_price} vs ref {ref_price} twin {twin:.2e} (div {div * 100:.3f}%)"
        assert redeem_diff <= REDEEM_TOL, \
            f"blk {block}: LL {ll_price} vs redemption {redeem} {redeem_diff:.2%} (div {div * 100:.3f}%)"
