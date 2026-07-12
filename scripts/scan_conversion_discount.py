#!/usr/bin/env python3
"""
Scan history: is the MULTIPLIER*fee swap discount (default 1.5x) enough for fee conversions to
always go through - and if not, what is?

The fee-conversion swap (PID/MerklPIDDriver._convert_fees and LTSwapZap.convert) sells the
market's BTC asset -> crvUSD with an on-chain floor
    min_dy = asset_out * price_oracle // 1e18 * (1 - MULTIPLIER*fee)   (fee is the pool's live fee)
and reverts/swallows if the pool can't meet it. price_oracle is the manipulation-resistant EMA,
so the risk is: during a fast BTC drop the EMA lags above spot and the executable price falls more
than MULTIPLIER*fee below it -> the swap can't meet min_dy. This samples every pool over its full
life and, per block, compares the true executable price (get_dy) to that floor. Reads are batched:
one Multicall3.aggregate3 per block (all live pools x {price_oracle, fee, get_dy, balances}),
fetch_multi'd. Each pool is scanned from its OWN deployment block (found by binary search; several
LT markets share one cryptopool, so pools are de-duped), and only blocks where the pool TVL >=
MIN_TVL (past the thin post-launch period) and the probe swap is a tiny fraction of it are counted.

Two numbers per pool:
  * actual on-chain result - does the contract's min_dy bind? NOTE the contract multiplies
    asset_out (native decimals) by price_oracle WITHOUT rescaling, so for 8-decimal assets
    (WBTC/cbBTC) min_dy comes out ~1e10x too small (effectively 0 - no swap protection at all);
    only the 18-decimal assets (tBTC/WETH) have a binding min_dy today.
  * decimal-correct headroom - the "required multiplier" m = (price_oracle - exec_price)/price_oracle
    / fee, i.e. how many fee-widths below the oracle the swap actually executed. MULTIPLIER is
    enough wherever m <= MULTIPLIER; the scan prints the MAX m (the minimum multiplier that would
    have covered every counted sample) - that is the answer to "what is enough".

Config below; reads NETWORK from scripts/networks.py. Run: python scripts/scan_conversion_discount.py
"""
import os
import csv
import json
import tempfile
import importlib.util
from datetime import datetime, timezone
import boa
from boa.rpc import EthereumRPC
from eth_abi import encode, decode
from eth_utils import keccak
from tqdm import tqdm

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
_spec = importlib.util.spec_from_file_location("_n", os.path.join(HERE, "networks.py"))
_m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_m)
NETWORK = _m.NETWORK

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
MARKET_IDS = list(range(0, 12))        # probe these factory indices; non-existent ones are skipped
FEE_DENOM = 10**10                     # Curve pool fee() scale
PRECISION = 10**18

MULTIPLIER = 2                         # discount = MULTIPLIER * pool_fee; THE knob - sweep to find enough
SWAP_FEE_MULTIPLIER = int(MULTIPLIER * PRECISION)   # 1e18-scaled, as the contracts store it

TARGET_NOTIONAL = 1_000                # probe swap size in crvUSD - kept tiny vs the pool (MIN_TVL/MAX_DX_FRAC)
MIN_TVL = 1_000_000                    # skip blocks where pool TVL < this ($): too close to launch / too thin
MAX_DX_FRAC = 0.005                    # and require the probe swap to be < this fraction of the pool

FLOOR_BLOCK = 22_000_000               # binary-search floor for a pool's deploy block (node serves past this)
MAX_LOOKBACK_DAYS = None               # cap history per pool (None = from each pool's deployment block)
BLOCKS_PER_DAY = 7200
STEP = 300                             # sample every STEP blocks (~1h); lower = catches shorter spikes
CHUNK = 500                            # per-block multicalls per JSON-RPC batch
OUT_CSV = os.path.join(tempfile.gettempdir(), "conversion_discount_scan.csv")

SEL_PO = keccak(text="price_oracle()")[:4]
SEL_FEE = keccak(text="fee()")[:4]
SEL_DY = keccak(text="get_dy(uint256,uint256,uint256)")[:4]
SEL_BAL = keccak(text="balances(uint256)")[:4]
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"   # same address on every chain
SEL_AGG3 = keccak(text="aggregate3((address,bool,bytes)[])")[:4]

POOL_ABI = json.dumps([
 {"name": "coins", "outputs": [{"type": "address"}], "inputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
 {"name": "price_oracle", "outputs": [{"type": "uint256"}], "inputs": [], "stateMutability": "view", "type": "function"},
])
ERC_ABI = json.dumps([
 {"name": "decimals", "outputs": [{"type": "uint8"}], "inputs": [], "stateMutability": "view", "type": "function"},
 {"name": "symbol", "outputs": [{"type": "string"}], "inputs": [], "stateMutability": "view", "type": "function"},
])


def _fetch_chunk(rpc, payloads):
    """Batch fetch; on any RPC error fall back to per-call so one bad call can't drop the chunk."""
    try:
        return rpc.fetch_multi(payloads)
    except Exception:
        out = []
        for method, params in payloads:
            try:
                out.append(rpc.fetch(method, params))
            except Exception:
                out.append(None)
        return out


def _has_code(rpc, addr, block):
    try:
        return len(rpc.fetch("eth_getCode", [addr, hex(block)])) > 2
    except Exception:
        return None                       # block not served


def _deploy_block(rpc, addr, lo, hi):
    """Smallest block in [lo, hi] where addr has code (binary search; addr has code at hi)."""
    if _has_code(rpc, addr, lo):
        return lo
    while lo < hi:
        mid = (lo + hi) // 2
        c = _has_code(rpc, addr, mid)
        if c:
            hi = mid
        else:
            lo = mid + 1                  # no code (or unserved) -> deployed later
    return lo


def load_pools(rpc, head):
    """Unique cryptopools behind the markets: asset/decimals, probe size dx, the market ids that
    share each pool (the migration reuses pools), and each pool's deployment block = its scan start."""
    by_addr = {}
    with boa.fork(NETWORK, block_identifier="latest"):
        factory = boa.load_partial(os.path.join(REPO, "contracts/Factory.vy")).at(FACTORY)
        lt_d = boa.load_partial(os.path.join(REPO, "contracts/LT.vy"))
        for i in MARKET_IDS:
            try:
                pool = boa.loads_abi(POOL_ABI).at(lt_d.at(factory.markets(i).lt).CRYPTOPOOL())
                if pool.coins(0).lower() != CRVUSD.lower():
                    continue              # conversion needs coin0 == crvUSD
                if pool.address in by_addr:
                    by_addr[pool.address]["markets"].append(i)
                    continue
                asset = boa.loads_abi(ERC_ABI).at(pool.coins(1))
                dec = asset.decimals()
                dx = TARGET_NOTIONAL * PRECISION * 10**dec // pool.price_oracle()   # ~TARGET_NOTIONAL of asset
                by_addr[pool.address] = dict(pool=pool.address, symbol=asset.symbol(), dec=dec, dx=dx, markets=[i])
            except Exception:
                continue                  # market index doesn't exist
    pools = list(by_addr.values())
    cap = head - MAX_LOOKBACK_DAYS * BLOCKS_PER_DAY if MAX_LOOKBACK_DAYS else 0
    for p in pools:
        p["start"] = max(_deploy_block(rpc, p["pool"], FLOOR_BLOCK, head), cap)
    return pools


def scan():
    rpc = EthereumRPC(NETWORK)
    head = int(rpc.fetch("eth_blockNumber", []), 16)
    pools = load_pools(rpc, head)
    for p in pools:
        print(f"  {p['symbol']:6s} markets {p['markets']}  pool {p['pool']}  start {p['start']}")

    # Per-pool constant 5-call block; per sampled block, include only the pools alive by then, and
    # bundle them into one Multicall3.aggregate3. fetch_multi JSON-RPC-batches those across blocks.
    pool_calls = [[(p["pool"], True, SEL_PO),
                   (p["pool"], True, SEL_FEE),
                   (p["pool"], True, SEL_DY + encode(["uint256"] * 3, [1, 0, p["dx"]])),
                   (p["pool"], True, SEL_BAL + encode(["uint256"], [0])),
                   (p["pool"], True, SEL_BAL + encode(["uint256"], [1]))] for p in pools]
    global_start = min(p["start"] for p in pools)
    payloads, blk_alive = [], []
    for b in range(global_start, head + 1, STEP):
        alive = [pi for pi, p in enumerate(pools) if p["start"] <= b]
        calls = [c for pi in alive for c in pool_calls[pi]]
        agg = SEL_AGG3 + encode(["(address,bool,bytes)[]"], [calls])
        payloads.append(("eth_call", [{"to": MULTICALL3, "data": "0x" + agg.hex()}, hex(b)]))
        blk_alive.append((b, alive))
    print(f"scanning {len(payloads)} blocks (up to {len(pools)} pools each) via Multicall3 "
          f"({global_start}..{head}, step {STEP})")

    results = []
    with tqdm(total=len(payloads), desc="blocks", unit="blk") as bar:
        for i in range(0, len(payloads), CHUNK):
            batch = payloads[i:i + CHUNK]
            results += _fetch_chunk(rpc, batch)
            bar.update(len(batch))

    rows = []
    for (b, alive), res in zip(blk_alive, results):
        if not res or len(res) <= 2:
            continue
        try:
            decoded = decode(["(bool,bytes)[]"], bytes.fromhex(res[2:]))[0]
        except Exception:
            continue
        for k, pi in enumerate(alive):
            p = pools[pi]
            oks_vals = decoded[5 * k:5 * k + 5]
            if not all(ok for ok, _ in oks_vals):
                continue
            p_o, fee, dy, bal0, bal1 = (int.from_bytes(v, "big") for _, v in oks_vals)
            if not p_o or not dy or not bal1:
                continue
            scale1 = 10 ** (18 - p["dec"])
            tvl = bal0 + bal1 * scale1 * p_o // PRECISION            # crvUSD (~USD), 1e18
            if tvl < MIN_TVL * PRECISION:
                continue                          # too close to launch / too thin
            if p["dx"] * scale1 * p_o // PRECISION > int(MAX_DX_FRAC * tvl):
                continue                          # probe not small enough vs the pool
            discount = min(SWAP_FEE_MULTIPLIER * fee // FEE_DENOM, PRECISION)
            min_dy = p["dx"] * p_o // PRECISION * (PRECISION - discount) // PRECISION
            p_exec = dy * PRECISION // (p["dx"] * scale1)            # crvUSD per whole asset, 1e18
            fee_f = fee / FEE_DENOM
            req_mult = ((p_o - p_exec) / p_o) / fee_f if fee_f else 0.0   # fee-widths below the oracle
            rows.append(dict(block=b, pool=p["pool"], symbol=p["symbol"], markets=str(p["markets"]),
                             tvl_musd=tvl / PRECISION / 1e6, p_o=p_o, fee_bp=fee_f * 1e4, dy=dy,
                             min_dy=min_dy, dy_over_min=dy / max(min_dy, 1), actual_pass=dy >= min_dy,
                             req_mult=req_mult))
    return pools, rows, rpc


def _date(rpc, block):
    ts = int(rpc.fetch("eth_getBlockByNumber", [hex(block), False])["timestamp"], 16)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def report(pools, rows, rpc):
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    overall = max(r["req_mult"] for r in rows)
    over_col = f">{MULTIPLIER}x"
    print(f"\n=== conversion-discount scan  (MULTIPLIER={MULTIPLIER}x, {len(rows)} samples, "
          f"TVL>=${MIN_TVL/1e6:.0f}M -> {OUT_CSV}) ===\n")
    print(f"{'markets':>12} {'asset':6} {'n':>6} {'scanned range':23} {'binds':7} "
          f"{'fails':>6} {'max req':>8} {'worst date':11} {over_col:>6}")
    for p in pools:
        pr = [r for r in rows if r["pool"] == p["pool"]]
        if not pr:
            continue
        fails = sum(not r["actual_pass"] for r in pr)
        worst = max(pr, key=lambda r: r["req_mult"])
        over = sum(r["req_mult"] > MULTIPLIER for r in pr)
        binds = "yes" if sum(r["dy_over_min"] for r in pr) / len(pr) < 100 else "no(~0)"
        rng = f"{_date(rpc, min(r['block'] for r in pr))}..{_date(rpc, max(r['block'] for r in pr))}"
        print(f"{str(p['markets']):>12} {p['symbol']:6} {len(pr):>6} {rng:23} {binds:7} "
              f"{fails:>6} {worst['req_mult']:>7.2f}x {_date(rpc, worst['block']):11} {over:>6}")

    print("\nmax req = fee-widths below the oracle the swap actually executed; MULTIPLIER covers a")
    print(f"sample when it stays <= MULTIPLIER. Each pool is scanned from its deploy block; only "
          f"TVL>=${MIN_TVL/1e6:.0f}M blocks with the probe < {MAX_DX_FRAC:.1%} of the pool are counted.")
    print(f"\nMINIMUM MULTIPLIER covering every counted sample: {overall:.2f}x  "
          f"(current MULTIPLIER={MULTIPLIER}x -> {'ENOUGH' if overall <= MULTIPLIER else 'NOT enough'})")


if __name__ == "__main__":
    _pools, _rows, _rpc = scan()
    if _rows:
        report(_pools, _rows, _rpc)
    else:
        print("no samples decoded (check MIN_TVL / RPC)")
