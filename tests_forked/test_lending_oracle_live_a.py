"""
The live-A oracles (YBLendingOracle / YBLendingOracleLL._live_curve) against the generation
deployed before them, on the real markets 7-10 at the chain head. Unless the pool has an A ramp
not yet observed by a trade, _live_curve reads the cached D at the A it was solved with, which
is what the deployed generation used (A at last_timestamp): the readings must be bit-identical.
A market whose pool has such a ramp live is skipped. The ramp behaviour itself is pinned by
tests/lt/test_lending_oracle_ramp.py.
"""
import warnings

import boa
import pytest
from tests_forked.networks import NETWORK

# Reading the deployed impl via the source ABI makes boa compare bytecodes; expected to differ.
warnings.filterwarnings("ignore", message="casted bytecode does not match compiled bytecode",
                        category=UserWarning)

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
PRE_LIVE_A_REF = "0x61BDFf6850ad7bBD67D3307d5c608C6e4cb3158f"       # YBLendingOracle singleton
PRE_LIVE_A_LL_IMPL = "0x786c7e47341E5967d64449bB383c53bef1FcbdD2"   # YBLendingOracleLL impl
MARKETS = [7, 8, 9, 10]
EMA_TIME = 866


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier="latest"):
        yield


@pytest.fixture(scope="module")
def deployers():
    return {
        "factory": boa.load_partial("contracts/Factory.vy"),
        "pool": boa.load_partial("contracts/twocrypto_pool/contracts/main/Twocrypto.vy"),
        "ref": boa.load_partial("contracts/utils/YBLendingOracle.vy"),
        "ll": boa.load_partial("contracts/utils/YBLendingOracleLL.vy"),
    }


@pytest.mark.parametrize("market_id", MARKETS, ids=[f"m{i}" for i in MARKETS])
def test_live_a_matches_deployed_without_ramp(deployers, market_id):
    m = deployers["factory"].at(FACTORY).markets(market_id)
    pool = deployers["pool"].at(m.cryptopool)
    if pool.future_A_gamma_time() > pool.last_timestamp():
        pytest.skip("pool has an unobserved A ramp: the generations legitimately differ")

    old_ref = deployers["ref"].at(PRE_LIVE_A_REF)
    new_ref = deployers["ref"].deploy(FACTORY, old_ref.PROXY_IMPL())
    for fn in ("price_in_asset", "price_in_usd", "staked_price_in_asset", "staked_price_in_usd"):
        for use_balances in (False, True):
            assert getattr(new_ref, fn)(m.lt, use_balances) == getattr(old_ref, fn)(m.lt, use_balances), (fn, use_balances)

    with boa.env.anchor():
        old_ll = deployers["ll"].at(PRE_LIVE_A_LL_IMPL)     # the uninitialized impl, bound here
        new_ll = deployers["ll"].deploy()
        for ll in (old_ll, new_ll):
            ll.initialize(m.lt, EMA_TIME, FACTORY)
        assert new_ll.price() == old_ll.price() and new_ll.price_usd() == old_ll.price_usd()   # raw
        assert new_ll.price_w() == old_ll.price_w()                                            # seed
        assert new_ll.fundamental_last() == old_ll.fundamental_last()
