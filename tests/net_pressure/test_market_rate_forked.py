"""
Forked test: MarketRateGetter against the live Sky Savings Rate (sUSDS).

Runs against ETH_RPC_URL if set, else tests_forked/networks.py NETWORK. Skips
cleanly if no Ethereum RPC is reachable (so it never breaks the default suite).
"""
import os
import boa
import pytest

try:
    from tests_forked.networks import NETWORK as _DEFAULT_RPC
except Exception:
    _DEFAULT_RPC = None

SUSDS = "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"  # mainnet sUSDS
RAY = 10**27
SECONDS_PER_YEAR = 365 * 86400


@pytest.fixture(scope="module")
def fork():
    rpc = os.environ.get("ETH_RPC_URL") or _DEFAULT_RPC
    if not rpc:
        pytest.skip("no Ethereum RPC configured")
    try:
        with boa.fork(rpc, block_identifier="latest"):
            yield
    except Exception as e:
        pytest.skip(f"fork RPC unreachable: {e}")


def test_market_rate_getter_live_susds(fork):
    getter = boa.load("contracts/net_pressure/MarketRateGetter.vy", SUSDS)

    rate = getter.rate()

    # Cross-check against the raw ssr conversion.
    ssr_abi = '[{"name":"ssr","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'
    ssr = boa.loads_abi(ssr_abi).at(SUSDS).ssr()
    expected = (ssr - RAY) * SECONDS_PER_YEAR // (RAY // 10**18)
    assert rate == expected

    # Sane savings rate: between 1% and 15% APR.
    assert 10**16 < rate < 15 * 10**16, f"unexpected sUSDS APR: {rate / 1e18}"
