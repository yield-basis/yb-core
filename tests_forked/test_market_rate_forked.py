"""
Forked test: MarketRateGetter against the live Sky Savings Rate (sUSDS), pinned to
the fixed block (conftest.FORK_BLOCK) so the numbers are reproducible run-to-run.
The autouse `forked_env` fixture in conftest.py does the fork.
"""
import boa

SUSDS = "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"  # mainnet sUSDS
RAY = 10**27
SECONDS_PER_YEAR = 365 * 86400


def test_market_rate_getter_live_susds(forked_env):
    getter = boa.load("contracts/net_pressure/MarketRateGetter.vy", SUSDS)

    rate = getter.rate()

    # Cross-check against the raw ssr conversion (self-validating at any block).
    ssr_abi = '[{"name":"ssr","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"}]'
    ssr = boa.loads_abi(ssr_abi).at(SUSDS).ssr()
    expected = (ssr - RAY) * SECONDS_PER_YEAR // (RAY // 10**18)
    assert rate == expected

    # Sane savings rate: between 1% and 15% APR.
    assert 10**16 < rate < 15 * 10**16, f"unexpected sUSDS APR: {rate / 1e18}"
