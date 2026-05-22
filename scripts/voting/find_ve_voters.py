#!/usr/bin/env python3
"""
Find the largest veYB voters by scanning VotingEscrow YB-locking (Deposit)
events, and print them as a pasteable Python list.

Used to seed the whale list that the forked dry-run of
create_vote_add_gauges_v3.py needs to pass the pending DAO votes.

Standalone, read-only - plain JSON-RPC, no fork.
"""
import json
import urllib.request

from eth_utils import keccak, to_checksum_address

from networks import NETWORK

VOTING_ESCROW = "0x8235c179e9e84688fbd8b12295efc26834dac211"
VE_DEPLOY_BLOCK = 23370927
DEPOSIT_TOPIC0 = "0x" + keccak(
    text="Deposit(address,address,uint256,uint256,uint256,uint256)"
).hex()
GET_VOTES_SELECTOR = "0x" + keccak(text="getVotes(address)").hex()[:8]
LOG_CHUNK = 10_000          # mainnet-cluster eth_getLogs range limit
TOP_N = 25


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


def get_votes(addr: str) -> int:
    data = GET_VOTES_SELECTOR + addr[2:].lower().rjust(64, "0")
    return int(rpc("eth_call", [{"to": VOTING_ESCROW, "data": data},
                                "latest"]), 16)


def main():
    latest = int(rpc("eth_blockNumber", []), 16)
    print(f"# scanning Deposit events {VE_DEPLOY_BLOCK}..{latest}")

    lockers = set()
    b = VE_DEPLOY_BLOCK
    while b <= latest:
        e = min(b + LOG_CHUNK - 1, latest)
        for log in rpc("eth_getLogs", [{
            "address": VOTING_ESCROW, "topics": [DEPOSIT_TOPIC0],
            "fromBlock": hex(b), "toBlock": hex(e),
        }]):
            lockers.add(to_checksum_address("0x" + log["topics"][2][26:]))
        b = e + 1

    voters = [(a, get_votes(a)) for a in lockers]
    voters = sorted((v for v in voters if v[1] > 0), key=lambda x: -x[1])
    total = sum(v for _, v in voters)
    print(f"# {len(lockers)} lockers, {len(voters)} with live voting power, "
          f"total {total / 1e18:,.0f} veYB\n")

    cum = 0
    print("TEST_VOTERS = [")
    for addr, power in voters[:TOP_N]:
        cum += power
        print(f'    "{addr}",  # {power / 1e18:>14,.0f} veYB  '
              f"({cum / total:6.2%} cumulative)")
    print("]")


if __name__ == "__main__":
    main()
