#!/usr/bin/env python3
"""
Find the HybridVault that holds the largest position in the old WETH YB
market (CURRENT_HYBRID_POOL_ID = 6) and print the owning user + the vault
address as a pasteable snippet.

Used to seed the forked --activate --test run of deploy_yb_pools_v3.py with
a real HybridVault withdrawal step — that exact production scenario is what
flips disabled_lts[lt_from] back to False and was breaking the legacy
LTMigrator deallocation path.

Standalone, read-only — plain JSON-RPC, no fork.
"""
import json
import urllib.request

from eth_utils import keccak, to_checksum_address

from networks import NETWORK

HYBRID_VAULT_FACTORY = "0xBdC32268851C324c6185809271dfe6d8dab8dC5b"
YB_FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
POOL_ID = 6                                  # old WETH market

# Conservative lower bound: a few months back is more than enough for v3.
# (eth_getLogs scans hex(lo)..hex(latest) regardless of contract age — if
# nothing was deployed yet we just get an empty page per chunk.)
SCAN_FROM_BLOCK = 22_000_000
LOG_CHUNK = 10_000                           # mainnet-cluster eth_getLogs limit

VAULT_CREATED_TOPIC0 = "0x" + keccak(
    text="VaultCreated(address,address)"
).hex()
BALANCE_OF_SELECTOR = "0x" + keccak(text="balanceOf(address)").hex()[:8]
MARKETS_SELECTOR = "0x" + keccak(text="markets(uint256)").hex()[:8]


def rpc(method, params):
    req = urllib.request.Request(
        NETWORK,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    r = json.loads(urllib.request.urlopen(req, timeout=120).read())
    if r.get("error"):
        raise RuntimeError(f"RPC {method}: {r['error']}")
    return r["result"]


def balance_of(token: str, who: str) -> int:
    data = BALANCE_OF_SELECTOR + who[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def market_lt_and_staker(pool_id: int) -> tuple[str, str]:
    """markets(uint256) returns Factory.Market = (asset_token, cryptopool,
    amm, lt, price_oracle, virtual_pool, staker). lt is at word 3, staker
    at word 6 in the returned ABI-encoded blob."""
    data = MARKETS_SELECTOR + hex(pool_id)[2:].rjust(64, "0")
    raw = rpc("eth_call", [{"to": YB_FACTORY, "data": data}, "latest"])
    blob = bytes.fromhex(raw[2:])
    lt = to_checksum_address(blob[3 * 32 + 12: 3 * 32 + 32])
    staker = to_checksum_address(blob[6 * 32 + 12: 6 * 32 + 32])
    return lt, staker


def main():
    latest = int(rpc("eth_blockNumber", []), 16)
    lt_addr, staker_addr = market_lt_and_staker(POOL_ID)
    print(f"# WETH market #{POOL_ID}: lt={lt_addr} staker={staker_addr}")
    print(f"# scanning VaultCreated events {SCAN_FROM_BLOCK}..{latest}")

    vaults = {}                              # vault -> user
    b = SCAN_FROM_BLOCK
    while b <= latest:
        e = min(b + LOG_CHUNK - 1, latest)
        for log in rpc("eth_getLogs", [{
            "address": HYBRID_VAULT_FACTORY,
            "topics": [VAULT_CREATED_TOPIC0],
            "fromBlock": hex(b), "toBlock": hex(e),
        }]):
            user = to_checksum_address("0x" + log["topics"][1][26:])
            vault = to_checksum_address("0x" + log["topics"][2][26:])
            vaults[vault] = user
        b = e + 1

    print(f"# {len(vaults)} HybridVault(s) discovered\n")

    holders = []
    for vault, user in vaults.items():
        lt_bal = balance_of(lt_addr, vault)
        st_bal = balance_of(staker_addr, vault)
        total = lt_bal + st_bal              # both denominated in LT shares
        if total > 0:
            holders.append((user, vault, lt_bal, st_bal, total))

    holders.sort(key=lambda h: -h[4])
    if not holders:
        print("# No HybridVault holds a WETH market position.")
        return

    print(f"# {len(holders)} vault(s) hold a WETH market position\n")
    for user, vault, lt_bal, st_bal, total in holders[:10]:
        print(f"#   user={user} vault={vault}")
        print(f"#     lt={lt_bal / 1e18:.6f}  staked={st_bal / 1e18:.6f}  "
              f"total={total / 1e18:.6f}")

    user, vault, lt_bal, st_bal, _ = holders[0]
    print(f"\nWETH_HYBRID_VAULT_USER  = \"{user}\"")
    print(f"WETH_HYBRID_VAULT       = \"{vault}\"")


if __name__ == "__main__":
    main()
