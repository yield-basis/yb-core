#!/usr/bin/env python3
"""
Print the initial (connect-time) bonus APR the deployed MerklPIDDriver would offer right now,
computed live from the deployed contracts in scripts/merkl_pid_deployment.json.

This is the same number the deploy script previews before the vote, but read straight off the
already-deployed system: it forks the recorded node at head and only calls view functions, so
it never broadcasts. The "initial" step is the clean-slate connect (integral=0, d_pressure=0,
prev_pressure=pressure, dt=0) with the sink pool's live TVL as the measured sink.

    python scripts/print_initial_apr.py
"""
import os
import json
import warnings
import boa

# .at() compares freshly compiled vs deployed bytecode; the mismatch is harmless (we only read).
warnings.filterwarnings("ignore", message="casted bytecode does not match compiled bytecode",
                        category=UserWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEPLOY_JSON = os.path.join(HERE, "merkl_pid_deployment.json")

SINK_POOL_ABI = json.dumps([
    {"name": "get_virtual_price", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "totalSupply", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
])


def cpath(rel):
    return os.path.join(REPO, rel)


def main():
    cfg = json.load(open(DEPLOY_JSON))
    with boa.fork(cfg["network"]):
        driver = boa.load_partial(cpath('contracts/net_pressure/MerklPIDDriver.vy')).at(cfg["merkl_pid_driver"])
        sink_pool = boa.loads_abi(SINK_POOL_ABI).at(cfg["sink_lp"])

        sig = driver.raw_signals()                     # pressure, half_tvl, market_rate (all 1e18)
        sink_tvl = sink_pool.totalSupply() * sink_pool.get_virtual_price() // 10**18
        # Clean-slate connect step, real measured sink.
        st = driver.preview_target_apr(sink_tvl, 0, sig.pressure, 0, 0)

        pressure_pct = sig.pressure / 1e18 * 100
        sink_pct = st.sink / 1e18 * 100
        market_pct = sig.market_rate / 1e18 * 100
        apr_pct = st.target_apr / 1e18 * 100
        multiple = (st.target_apr / sig.market_rate + 1) if sig.market_rate else 0.0

        print(f"MerklPIDDriver {cfg['merkl_pid_driver']} (connected={driver.connected()})")
        print(f"  net pressure       : {pressure_pct:.4f}% of half-TVL")
        print(f"  sink (pool TVL)    : {sink_tvl/1e18:,.0f} crvUSD  ({sink_pct:.4f}% of half-TVL)")
        print(f"  market rate (sUSDS): {market_pct:.3f}% APR")
        print(f"  => INITIAL BONUS APR: {apr_pct:.3f}%   ({multiple:.2f}x market)")


if __name__ == "__main__":
    main()
