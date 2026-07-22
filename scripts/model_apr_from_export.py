#!/usr/bin/env python3
"""
Replay the MerklPIDDriver as a *system* over the exported signals and check we reproduce the
TSV points.

The export (scripts/data/ybExport.tsv) is what Merkl persisted stepping the PID off-chain: at
each window it measured the sink TVL, read pressure/market live, and called preview_target_apr,
saving the returned (integral, prev_pressure, d_pressure). This script reconstructs that loop:

  - map each window_end_ts to the on-chain block that was head at that time (binary search),
  - read the driver at that historical block via eth_call, so preview_target_apr sees the same
    pressure / half-TVL / market rate Merkl saw,
  - measure the sink as the FULL pool TVL (totalSupply * get_virtual_price), not just staked,
  - carry (integral, prev_pressure, d_pressure) forward step to step (closed loop),

then diff the computed state against the TSV row. dt for each step is the wall gap between
consecutive windows; the first step is the clean-slate connect (dt=0, prev_pressure=pressure).

No forks: every read is a raw eth_call pinned to the row's block, and the two co-located sink
reads (plus raw_signals on the connect step) are batched through Multicall3 in a single call.
preview_target_apr is the one dependent call per block - its sink_tvl argument comes from the
multicall result - so it follows as a second eth_call at the same block.

    python scripts/model_apr_from_export.py [path/to/ybExport.tsv] [max_rows]
"""
import os
import sys
import json
from boa.rpc import EthereumRPC
from eth_abi import encode as abi_encode, decode as abi_decode
from eth_utils import keccak

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY_JSON = os.path.join(HERE, "merkl_pid_deployment.json")
DEFAULT_TSV = os.path.join(HERE, "data", "ybExport.tsv")

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"


def _sel(sig):
    return keccak(text=sig)[:4]


# ABIs / selectors compiled once, outside the loop.
SEL_TOTAL_SUPPLY = _sel("totalSupply()")
SEL_VIRTUAL_PRICE = _sel("get_virtual_price()")
SEL_RAW_SIGNALS = _sel("raw_signals()")                                              # -> (pressure, half_tvl, market_rate)
SEL_PREVIEW = _sel("preview_target_apr(uint256,int256,uint256,int256,uint256)")      # -> AprState
SEL_AGGREGATE = _sel("aggregate((address,bytes)[])")                                 # Multicall3 -> (blockNumber, bytes[])
APR_STATE_TYPES = ["uint256", "int256", "uint256", "int256", "uint256", "uint256"]   # target_apr, integral, prev_pressure, d_pressure, pressure, sink


def _int(cell):
    return int(cell.replace(",", "").strip())


def load_rows(tsv_path):
    rows = []
    with open(tsv_path) as f:
        for line in f:
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            c = line.split("\t")
            rows.append({
                "idx": _int(c[0]), "window_end_ts": _int(c[1]), "window_end": c[2].strip(),
                "integral": _int(c[3]), "prev_pressure": _int(c[4]), "d_pressure": _int(c[5]),
                "connected": c[6].strip().lower() == "true",
            })
    if not rows:
        raise SystemExit(f"No data rows in {tsv_path}")
    return rows


def main():
    tsv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TSV
    max_rows = int(sys.argv[2]) if len(sys.argv) > 2 else None
    rows = load_rows(tsv_path)
    if max_rows:
        rows = rows[:max_rows]

    cfg = json.load(open(DEPLOY_JSON))
    driver = cfg["merkl_pid_driver"]
    sink = cfg["sink_lp"]
    rpc = EthereumRPC(cfg["network"])

    def eth_call(to, data, block):
        return bytes.fromhex(rpc.fetch("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])[2:])

    def multicall(calls, block):
        """calls: list of (target, calldata_bytes) -> list of return-bytes, all at `block`."""
        payload = SEL_AGGREGATE + abi_encode(["(address,bytes)[]"], [calls])
        _, rets = abi_decode(["uint256", "bytes[]"], eth_call(MULTICALL3, payload, block))
        return rets

    # --- timestamp -> block mapping (floor: largest block with ts <= target) ---
    ts_cache = {}

    def block_ts(n):
        if n not in ts_cache:
            ts_cache[n] = int(rpc.fetch("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
        return ts_cache[n]

    head_n = int(rpc.fetch("eth_getBlockByNumber", ["latest", False])["number"], 16)

    first_ts = rows[0]["window_end_ts"]
    lo = head_n
    step = 1024
    while block_ts(lo) > first_ts:
        lo = max(0, lo - step)
        step *= 2

    def block_at(target, lo):
        # Expand hi from lo (windows are ~140 blocks apart) instead of searching to head_n.
        hi = min(lo + 256, head_n)
        span = 256
        while hi < head_n and block_ts(hi) <= target:
            lo, span = hi, span * 2
            hi = min(hi + span, head_n)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if block_ts(mid) <= target:
                lo = mid
            else:
                hi = mid - 1
        return lo

    search_lo = lo
    for r in rows:
        r["block"] = block_at(r["window_end_ts"], search_lo)
        search_lo = r["block"]
    print(f"Mapped {len(rows)} windows to blocks "
          f"[{rows[0]['block']} .. {rows[-1]['block']}], head {head_n}")

    # --- closed-loop replay through the on-chain step ---
    integral = 0
    prev_pressure = 0
    d_pressure = 0
    prev_ts = None

    hdr = f"{'#':>3} {'block':>9} {'dt':>6} | {'p_tsv':>9} {'p_calc':>9} | {'D_tsv':>10} {'D_calc':>10} | {'I_tsv':>10} {'I_calc':>10} | apr%"
    print(hdr)
    print("-" * len(hdr))

    max_err = {"prev_pressure": 0, "d_pressure": 0, "integral": 0}

    for r in rows:
        blk = hex(r["block"])
        dt = 0 if prev_ts is None else r["window_end_ts"] - prev_ts

        # Batch the co-located reads at this block: sink TVL, plus raw_signals on the connect step.
        calls = [(sink, SEL_TOTAL_SUPPLY), (sink, SEL_VIRTUAL_PRICE)]
        if prev_ts is None:
            calls.append((driver, SEL_RAW_SIGNALS))
        rets = multicall(calls, blk)
        sink_tvl = int.from_bytes(rets[0], "big") * int.from_bytes(rets[1], "big") // 10**18
        # Clean-slate connect on the first step: prev_pressure_in = measured pressure.
        pp_in = abi_decode(["uint256", "uint256", "uint256"], rets[2])[0] if prev_ts is None else prev_pressure

        # Dependent call (needs sink_tvl from the multicall), so it follows at the same block.
        preview_data = SEL_PREVIEW + abi_encode(
            ["uint256", "int256", "uint256", "int256", "uint256"],
            [sink_tvl, integral, pp_in, d_pressure, dt])
        target_apr, integral, prev_pressure, d_pressure, _p, _sink = abi_decode(
            APR_STATE_TYPES, eth_call(driver, preview_data, blk))

        prev_ts = r["window_end_ts"]

        e_p = abs(prev_pressure - r["prev_pressure"])
        e_d = abs(d_pressure - r["d_pressure"])
        e_i = abs(integral - r["integral"])
        max_err["prev_pressure"] = max(max_err["prev_pressure"], e_p)
        max_err["d_pressure"] = max(max_err["d_pressure"], e_d)
        max_err["integral"] = max(max_err["integral"], e_i)

        flag = "" if (e_p == 0 and e_d == 0 and e_i == 0) else "  <- diff"
        print(f"{r['idx']:>3} {r['block']:>9} {dt:>6} | "
              f"{r['prev_pressure']/1e18:>9.6f} {prev_pressure/1e18:>9.6f} | "
              f"{r['d_pressure']/1e18:>10.5f} {d_pressure/1e18:>10.5f} | "
              f"{r['integral']/1e18:>10.7f} {integral/1e18:>10.7f} | "
              f"{target_apr/1e18*100:5.2f}{flag}")

    print("-" * len(hdr))
    print("Max |computed - TSV| (1e18 units):")
    for k, v in max_err.items():
        print(f"  {k:>14}: {v}  ({v/1e18:.3e})")


if __name__ == "__main__":
    main()
