#!/usr/bin/env python3
"""
Print the bonus APR the deployed MerklPIDDriver would offer at the *next* step, given the PID
state already accumulated by the running (test-mode) measurements exported to a TSV.

Where print_initial_apr.py shows the clean-slate connect number (integral=0, d_pressure=0,
prev_pressure=pressure, dt=0), this reads the latest persisted state (integral, prev_pressure,
d_pressure) straight out of Merkl's export and feeds it into the same on-chain preview:

    preview_target_apr(sink_tvl, integral, prev_pressure, d_pressure, dt)

dt is the wall time since the last exported window (fork head - last window_end_ts). Pressure,
sink, market rate and gains are all read live off the forked deployment; it only calls view
functions, so it never broadcasts.

    python scripts/print_apr_from_export.py [path/to/ybExport.tsv]
"""
import os
import sys
import json
import warnings
import boa

# .at() compares freshly compiled vs deployed bytecode; the mismatch is harmless (we only read).
warnings.filterwarnings("ignore", message="casted bytecode does not match compiled bytecode",
                        category=UserWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEPLOY_JSON = os.path.join(HERE, "merkl_pid_deployment.json")
DEFAULT_TSV = os.path.join(HERE, "data", "ybExport.tsv")

SINK_POOL_ABI = json.dumps([
    {"name": "get_virtual_price", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "totalSupply", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
])


def cpath(rel):
    return os.path.join(REPO, rel)


def _int(cell):
    return int(cell.replace(",", "").strip())


def load_last_row(tsv_path):
    """Parse the export and return the last data row as a dict of the persisted PID state."""
    rows = []
    with open(tsv_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            cols = line.split("\t")
            # idx, window_end_ts, window_end(str), integral, prev_pressure, d_pressure, connected
            rows.append({
                "idx": _int(cols[0]),
                "window_end_ts": _int(cols[1]),
                "window_end": cols[2].strip(),
                "integral": _int(cols[3]),
                "prev_pressure": _int(cols[4]),
                "d_pressure": _int(cols[5]),
                "connected": cols[6].strip().lower() == "true",
            })
    if not rows:
        raise SystemExit(f"No data rows found in {tsv_path}")
    return rows[-1], len(rows)


def main():
    tsv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TSV
    row, n_rows = load_last_row(tsv_path)

    cfg = json.load(open(DEPLOY_JSON))
    with boa.fork(cfg["network"], block_identifier="latest"):
        head_ts = boa.env.evm.patch.timestamp
        driver = boa.load_partial(cpath('contracts/net_pressure/MerklPIDDriver.vy')).at(cfg["merkl_pid_driver"])
        sink_pool = boa.loads_abi(SINK_POOL_ABI).at(cfg["sink_lp"])

        sig = driver.raw_signals()                     # pressure, half_tvl, market_rate (all 1e18)
        sink_tvl = sink_pool.totalSupply() * sink_pool.get_virtual_price() // 10**18

        # dt = wall time since the last exported window. Guard against a fork head that predates
        # the export (fall back to nominal 30-min cadence) so we never underflow uint256 dt.
        dt = head_ts - row["window_end_ts"]
        if dt < 0:
            dt = 30 * 60

        # Measured-state step: replay Merkl's next call with the persisted (I, prev_p, D) state.
        st = driver.preview_target_apr(sink_tvl, row["integral"], row["prev_pressure"], row["d_pressure"], dt)
        # Clean-slate connect step, for comparison with print_initial_apr.py.
        st0 = driver.preview_target_apr(sink_tvl, 0, sig.pressure, 0, 0)

        def mult(apr):
            return (apr / sig.market_rate + 1) if sig.market_rate else 0.0

        print(f"MerklPIDDriver {cfg['merkl_pid_driver']} (connected={driver.connected()})")
        print(f"export             : {tsv_path}")
        print(f"  last row #{row['idx']} of {n_rows}, window_end {row['window_end']}")
        print(f"  persisted integral    : {row['integral'] / 1e18:.6f}")
        print(f"  persisted prev_pressure: {row['prev_pressure'] / 1e18 * 100:.4f}%  (live now {sig.pressure / 1e18 * 100:.4f}%)")
        print(f"  persisted d_pressure  : {row['d_pressure'] / 1e18:.4f}")
        print(f"  dt since last window  : {dt} s ({dt / 60:.1f} min)")
        print("  ---")
        print(f"  live pressure         : {sig.pressure / 1e18 * 100:.4f}% of half-TVL")
        print(f"  live sink (pool TVL)  : {sink_tvl / 1e18:,.0f} crvUSD  ({st.sink / 1e18 * 100:.4f}% of half-TVL)")
        print(f"  live market (sUSDS)   : {sig.market_rate / 1e18 * 100:.3f}% APR")
        print("  ---")
        print(f"  INITIAL (clean-slate) bonus APR: {st0.target_apr / 1e18 * 100:.3f}%   ({mult(st0.target_apr):.2f}x market)")
        print(f"  => APR FROM MEASURED STATE     : {st.target_apr / 1e18 * 100:.3f}%   ({mult(st.target_apr):.2f}x market)")
        print(f"     next state to persist: integral={st.integral / 1e18:.6f}, "
              f"prev_pressure={st.prev_pressure / 1e18 * 100:.4f}%, d_pressure={st.d_pressure / 1e18:.4f}")


if __name__ == "__main__":
    main()
