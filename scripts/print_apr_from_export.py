#!/usr/bin/env python3
"""
Print the bonus APR the deployed MerklPIDDriver offered at every exported window, plus the
next-step number it would offer now.

Each TSV row holds the PID state Merkl persisted at that window (integral, prev_pressure,
d_pressure). For the per-row APR we replay the step the driver ran *for that window*: at the
row's historical block we call

    preview_target_apr(sink_tvl, integral_in, prev_pressure_in, d_pressure_in, dt)

with the *previous* row's persisted state as input (the first row is the clean-slate connect:
integral=0, d_pressure=0, prev_pressure=measured pressure, dt=0). pressure / half-TVL / market
rate are read live at that block; the sink is the FULL pool TVL (totalSupply * get_virtual_price).
Because each row's input comes straight from the export (open loop), the reads are independent
and pulled in two batched fetch_multi rounds rather than one call per row. All calls are views.

The trailing summary applies the *latest* row's state at chain head (dt = head - last window),
next to the clean-slate number from print_initial_apr.py.

    python scripts/print_apr_from_export.py [path/to/ybExport.tsv]
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


def _agg_calldata(calls):
    return SEL_AGGREGATE + abi_encode(["(address,bytes)[]"], [calls])


def _preview_calldata(sink_tvl, integral, prev_pressure, d_pressure, dt):
    return SEL_PREVIEW + abi_encode(
        ["uint256", "int256", "uint256", "int256", "uint256"],
        [sink_tvl, integral, prev_pressure, d_pressure, dt])


def main():
    tsv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TSV
    rows = load_rows(tsv_path)

    cfg = json.load(open(DEPLOY_JSON))
    driver = cfg["merkl_pid_driver"]
    sink = cfg["sink_lp"]
    rpc = EthereumRPC(cfg["network"])

    def eth_call(to, data, block):
        return bytes.fromhex(rpc.fetch("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])[2:])

    def batch_eth_call(items):
        """items: list of (to, data_bytes, block) -> list of return-bytes (one fetch_multi)."""
        payloads = [("eth_call", [{"to": to, "data": "0x" + data.hex()}, block]) for to, data, block in items]
        return [bytes.fromhex(r[2:]) for r in rpc.fetch_multi(payloads)]

    # --- timestamp -> block mapping (floor: largest block with ts <= target) ---
    ts_cache = {}

    def block_ts(n):
        if n not in ts_cache:
            ts_cache[n] = int(rpc.fetch("eth_getBlockByNumber", [hex(n), False])["timestamp"], 16)
        return ts_cache[n]

    head = rpc.fetch("eth_getBlockByNumber", ["latest", False])
    head_n = int(head["number"], 16)
    head_ts = int(head["timestamp"], 16)

    first_ts = rows[0]["window_end_ts"]
    lo = head_n
    step = 1024
    while block_ts(lo) > first_ts:
        lo = max(0, lo - step)
        step *= 2

    def block_at(target, lo):
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

    # --- Round 1 (batched): sink TVL + market rate + measured pressure at each row's block ---
    reads = batch_eth_call([
        (MULTICALL3, _agg_calldata([(sink, SEL_TOTAL_SUPPLY), (sink, SEL_VIRTUAL_PRICE), (driver, SEL_RAW_SIGNALS)]), hex(r["block"]))
        for r in rows
    ])
    for r, ret in zip(rows, reads):
        _, (ts_ret, vp_ret, rs_ret) = abi_decode(["uint256", "bytes[]"], ret)
        r["sink_tvl"] = int.from_bytes(ts_ret, "big") * int.from_bytes(vp_ret, "big") // 10**18
        _, _, r["market_rate"] = abi_decode(["uint256", "uint256", "uint256"], rs_ret)

    # --- Round 2 (batched): the step each window ran (input = previous row's persisted state) ---
    preview_items = []
    for i, r in enumerate(rows):
        if i == 0:  # clean-slate connect: prev_pressure_in = measured pressure this block
            _, prs = abi_decode(["uint256", "bytes[]"], reads[0])
            in_i, in_pp, in_dp, dt = 0, abi_decode(["uint256", "uint256", "uint256"], prs[2])[0], 0, 0
        else:
            prev = rows[i - 1]
            in_i, in_pp, in_dp = prev["integral"], prev["prev_pressure"], prev["d_pressure"]
            dt = r["window_end_ts"] - prev["window_end_ts"]
        preview_items.append((driver, _preview_calldata(r["sink_tvl"], in_i, in_pp, in_dp, dt), hex(r["block"])))
    previews = batch_eth_call(preview_items)

    # --- per-row table ---
    print(f"MerklPIDDriver {driver}")
    print(f"export {tsv_path}: {len(rows)} windows, blocks [{rows[0]['block']} .. {rows[-1]['block']}]")
    hdr = f"{'#':>3} {'window_end':>19} {'block':>9} | {'press%':>7} {'sink%':>7} {'mkt%':>6} | {'bonusAPR%':>9} {'xmkt':>5}"
    print(hdr)
    print("-" * len(hdr))
    for r, ret in zip(rows, previews):
        apr, _integral, _pp, _dp, pressure, sink_frac = abi_decode(APR_STATE_TYPES, ret)
        mkt = r["market_rate"]
        xmkt = (apr / mkt + 1) if mkt else 0.0
        print(f"{r['idx']:>3} {r['window_end'][:19]:>19} {r['block']:>9} | "
              f"{pressure/1e18*100:>6.3f}% {sink_frac/1e18*100:>6.3f}% {mkt/1e18*100:>5.2f}% | "
              f"{apr/1e18*100:>8.3f}% {xmkt:>4.2f}x")

    # --- trailing summary: latest state at chain head, vs clean-slate ---
    last = rows[-1]
    hb = hex(head_n)
    _, (ts_ret, vp_ret, rs_ret) = abi_decode(["uint256", "bytes[]"], eth_call(
        MULTICALL3, _agg_calldata([(sink, SEL_TOTAL_SUPPLY), (sink, SEL_VIRTUAL_PRICE), (driver, SEL_RAW_SIGNALS)]), hb))
    sink_tvl = int.from_bytes(ts_ret, "big") * int.from_bytes(vp_ret, "big") // 10**18
    live_pressure, _half_tvl, market_rate = abi_decode(["uint256", "uint256", "uint256"], rs_ret)
    dt = max(head_ts - last["window_end_ts"], 30 * 60)

    st = abi_decode(APR_STATE_TYPES, eth_call(
        driver, _preview_calldata(sink_tvl, last["integral"], last["prev_pressure"], last["d_pressure"], dt), hb))
    st0 = abi_decode(APR_STATE_TYPES, eth_call(
        driver, _preview_calldata(sink_tvl, 0, live_pressure, 0, 0), hb))

    def mult(apr):
        return (apr / market_rate + 1) if market_rate else 0.0

    print("-" * len(hdr))
    print(f"latest state (row #{last['idx']}) at head block {head_n}, dt={dt}s ({dt/60:.0f} min):")
    print(f"  live pressure {live_pressure/1e18*100:.4f}%  sink {st[5]/1e18*100:.4f}%  market {market_rate/1e18*100:.3f}%")
    print(f"  INITIAL (clean-slate) bonus APR: {st0[0]/1e18*100:.3f}%   ({mult(st0[0]):.2f}x market)")
    print(f"  => APR FROM MEASURED STATE     : {st[0]/1e18*100:.3f}%   ({mult(st[0]):.2f}x market)")


if __name__ == "__main__":
    main()
