#!/usr/bin/env python3
"""
Find every veYB holder who still has an active vote_user_slopes entry on the
now-obsolete YB market gauges 3, 4, 5 (Factory.markets[3..5].staker), and
report their current voting allocation - how much of their veYB is still
parked on the obsolete gauges vs how much they have moved to live ones.

Scans VoteForGauge events on the GaugeController, collects every user who
ever voted on an obsolete gauge, and then re-scans the full log to gather
every other gauge those same users ever touched. For each (user, gauge) we
read vote_user_slopes(user, gauge).power to get the CURRENT weight (callers
that revoted to weight=0 are filtered out) and split totals into "obsolete"
vs "other live gauges" buckets. vote_user_power(user) is the on-chain total
weight used; the remainder is reported as `idle` and is power the user
could still allocate elsewhere. veYB voting power comes from
VotingEscrow.getVotes(user) at head.

Standalone, read-only - plain JSON-RPC against the local mainnet-cluster node.
"""
import json
import urllib.request

from eth_utils import keccak, to_checksum_address

from networks import NETWORK


GAUGE_CONTROLLER = "0x1Be14811A3a06F6aF4fA64310a636e1Df04c1c21"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
VOTING_ESCROW = "0x8235c179e9e84688fbd8b12295efc26834dac211"

# Same starting block as scripts/voting/find_ve_voters.py - GC and VE were
# deployed in the same window, so this safely covers all VoteForGauge logs.
GC_START_BLOCK = 23370927

OBSOLETE_MARKET_IDS = [3, 4, 5]

LOG_CHUNK = 10_000

VOTE_TOPIC0 = "0x" + keccak(
    text="VoteForGauge(uint256,address,address,uint256)"
).hex()
GET_VOTES_SEL = "0x" + keccak(text="getVotes(address)").hex()[:8]
VOTE_USER_SLOPES_SEL = "0x" + keccak(
    text="vote_user_slopes(address,address)"
).hex()[:8]
VOTE_USER_POWER_SEL = "0x" + keccak(text="vote_user_power(address)").hex()[:8]
LAST_USER_VOTE_SEL = "0x" + keccak(
    text="last_user_vote(address,address)"
).hex()[:8]
LOCKED_END_SEL = "0x" + keccak(text="locked__end(address)").hex()[:8]
MARKETS_SEL = "0x" + keccak(text="markets(uint256)").hex()[:8]
GAUGES_SEL = "0x" + keccak(text="gauges(uint256)").hex()[:8]
N_GAUGES_SEL = "0x" + keccak(text="n_gauges()").hex()[:8]


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


def market_staker(market_id: int) -> str:
    data = MARKETS_SEL + market_id.to_bytes(32, "big").hex()
    raw = bytes.fromhex(
        rpc("eth_call", [{"to": FACTORY, "data": data}, "latest"])[2:]
    )
    # Market struct: amm, lt, virtual_pool, asset_token, collateral_token,
    # price_oracle, staker, ... -> staker is field index 6
    return to_checksum_address("0x" + raw[6 * 32:7 * 32].hex()[-40:])


def call_uint(to: str, selector: str, *args: str) -> int:
    data = selector + "".join(a[2:].lower().rjust(64, "0") for a in args)
    return int(rpc("eth_call", [{"to": to, "data": data}, "latest"]), 16)


def get_votes(user: str) -> int:
    return call_uint(VOTING_ESCROW, GET_VOTES_SEL, user)


def locked_end(user: str) -> int:
    return call_uint(VOTING_ESCROW, LOCKED_END_SEL, user)


def vote_user_slopes(user: str, gauge: str) -> dict:
    data = (
        VOTE_USER_SLOPES_SEL
        + user[2:].lower().rjust(64, "0")
        + gauge[2:].lower().rjust(64, "0")
    )
    raw = bytes.fromhex(
        rpc("eth_call", [{"to": GAUGE_CONTROLLER, "data": data}, "latest"])[2:]
    )
    return {
        "slope": int.from_bytes(raw[0:32], "big"),
        "bias": int.from_bytes(raw[32:64], "big"),
        "power": int.from_bytes(raw[64:96], "big"),
        "end": int.from_bytes(raw[96:128], "big"),
    }


def last_user_vote(user: str, gauge: str) -> int:
    data = (
        LAST_USER_VOTE_SEL
        + user[2:].lower().rjust(64, "0")
        + gauge[2:].lower().rjust(64, "0")
    )
    return int(
        rpc("eth_call", [{"to": GAUGE_CONTROLLER, "data": data}, "latest"]), 16
    )


def all_vote_events(latest: int):
    """Yield (user, gauge) pairs from every VoteForGauge event in range."""
    b = GC_START_BLOCK
    while b <= latest:
        e = min(b + LOG_CHUNK - 1, latest)
        for log in rpc("eth_getLogs", [{
            "address": GAUGE_CONTROLLER,
            "topics": [VOTE_TOPIC0],
            "fromBlock": hex(b),
            "toBlock": hex(e),
        }]):
            data = bytes.fromhex(log["data"][2:])
            # data = time(32) | user(32) | gauge_addr(32) | weight(32)
            user = to_checksum_address("0x" + data[32:64].hex()[-40:])
            gauge = to_checksum_address("0x" + data[64:96].hex()[-40:])
            yield user, gauge
        b = e + 1


def vote_user_power(user: str) -> int:
    return call_uint(GAUGE_CONTROLLER, VOTE_USER_POWER_SEL, user)


def list_all_gauges() -> dict:
    """Return {gauge_address: index_in_gauges_array} for every live gauge."""
    n = int(rpc(
        "eth_call",
        [{"to": GAUGE_CONTROLLER, "data": N_GAUGES_SEL}, "latest"]
    ), 16)
    out = {}
    for i in range(n):
        data = GAUGES_SEL + i.to_bytes(32, "big").hex()
        addr = to_checksum_address(
            "0x" + rpc(
                "eth_call",
                [{"to": GAUGE_CONTROLLER, "data": data}, "latest"]
            )[-40:]
        )
        out[addr] = i
    return out


def main():
    latest = int(rpc("eth_blockNumber", []), 16)

    print(f"Resolving obsolete gauges (markets {OBSOLETE_MARKET_IDS}) at"
          f" block {latest}\n")
    gauge_for_market = {m: market_staker(m) for m in OBSOLETE_MARKET_IDS}
    market_for_gauge = {g: m for m, g in gauge_for_market.items()}
    for m, g in gauge_for_market.items():
        print(f"  market {m}: gauge {g}")
    obsolete = set(gauge_for_market.values())
    print()

    print(f"Indexing live gauges from GaugeController.gauges()")
    gauge_index = list_all_gauges()
    print(f"  {len(gauge_index)} gauges registered\n")

    print(f"Scanning VoteForGauge events {GC_START_BLOCK}..{latest}"
          f" (one pass; tracking everyone who ever touched the obsolete set)")
    # First identify the candidates (users who ever voted on an obsolete
    # gauge); in the same pass record every gauge each candidate ever voted on
    # so we can later poll the live state for the full breakdown.
    candidates: set = set()
    ever_voted_on = {}  # user -> set(gauge)
    pending = []        # buffer of (user, gauge) we may add to ever_voted_on
    # one streaming pass: we only learn a user is a candidate once we see
    # their first vote on an obsolete gauge, so we buffer earlier events.
    for user, gauge in all_vote_events(latest):
        if gauge in obsolete:
            candidates.add(user)
        pending.append((user, gauge))

    for user, gauge in pending:
        if user in candidates:
            ever_voted_on.setdefault(user, set()).add(gauge)

    print(f"  {len(candidates)} candidates (ever voted on obsolete)\n")

    # Poll live state per (candidate, gauge_they_ever_voted_on)
    rows = []  # one per (user, gauge) with nonzero current power
    user_totals = {}  # user -> {"obsolete": int, "other": int, "other_list": []}
    for user in sorted(candidates):
        bucket = {"obsolete": 0, "other": 0, "other_list": []}
        for gauge in ever_voted_on[user]:
            slope = vote_user_slopes(user, gauge)
            p = slope["power"]
            if p == 0:
                continue
            if gauge in obsolete:
                bucket["obsolete"] += p
                rows.append({
                    "user": user,
                    "gauge": gauge,
                    "market": market_for_gauge[gauge],
                    "power": p,
                    "vote_end": slope["end"],
                    "last_vote": last_user_vote(user, gauge),
                })
            else:
                bucket["other"] += p
                idx = gauge_index.get(gauge, "?")
                bucket["other_list"].append((idx, gauge, p))
        bucket["total"] = vote_user_power(user)
        bucket["ve_balance"] = get_votes(user)
        bucket["lock_end"] = locked_end(user)
        user_totals[user] = bucket

    # Filter to users with currently nonzero power on an obsolete gauge
    active_users = {u for u, b in user_totals.items() if b["obsolete"] > 0}

    print(f"Active votes on obsolete gauges:"
          f" {len(rows)} (user, gauge) pairs,"
          f" {len(active_users)} unique users,"
          f" total {sum(user_totals[u]['ve_balance'] for u in active_users) / 1e18:,.0f} veYB"
          f" of live voting power\n")

    # Per-gauge totals
    print("Per-gauge totals:")
    for g in sorted(obsolete):
        ve_per_user = {
            r["user"]: user_totals[r["user"]]["ve_balance"]
            for r in rows if r["gauge"] == g
        }
        print(f"  market {market_for_gauge[g]} ({g}):"
              f" {len(ve_per_user)} voters,"
              f" {sum(ve_per_user.values()) / 1e18:,.0f} veYB")
    print()

    # Per-user table: how much of their power is still on obsolete vs moved
    sorted_users = sorted(
        active_users,
        key=lambda u: -user_totals[u]["ve_balance"],
    )
    print("Per-user breakdown (sorted by veYB):\n")
    print(f"{'#':>3}  {'user':<44}  "
          f"{'obs%':>6}  {'other%':>7}  {'idle%':>6}  {'total%':>7}  "
          f"{'veYB':>14}  other gauges (idx:weight%)")
    print("-" * 140)
    obsolete_only = 0
    obsolete_only_ve = 0
    for rank, u in enumerate(sorted_users, 1):
        b = user_totals[u]
        idle = b["total"] - b["obsolete"] - b["other"]
        other_str = ", ".join(
            f"g{idx}:{p / 100:.1f}%"
            for idx, _, p in sorted(b["other_list"])
        ) or "-"
        if b["other"] == 0:
            obsolete_only += 1
            obsolete_only_ve += b["ve_balance"]
        print(
            f"{rank:>3}  {u:<44}  "
            f"{b['obsolete'] / 100:>5.1f}%  "
            f"{b['other'] / 100:>6.1f}%  "
            f"{idle / 100:>5.1f}%  "
            f"{b['total'] / 100:>6.1f}%  "
            f"{b['ve_balance'] / 1e18:>14,.2f}  "
            f"{other_str}"
        )

    print()
    print(f"Of the {len(active_users)} users still on obsolete gauges,"
          f" {obsolete_only} have NOT voted for any other live gauge"
          f" ({obsolete_only_ve / 1e18:,.0f} veYB locked entirely on obsolete).")

    # Old per-(user, gauge) listing for backwards compat
    rows.sort(key=lambda r: -user_totals[r["user"]]["ve_balance"])
    print()
    print("Raw (user, gauge) rows with nonzero current power on an obsolete"
          " gauge:\n")
    print(f"{'user':<44}  {'market':>6}  {'weight':>8}  "
          f"{'veYB':>14}  {'lock_end':>10}")
    print("-" * 92)
    for r in rows:
        b = user_totals[r["user"]]
        print(
            f"{r['user']:<44}  {r['market']:>6}  "
            f"{r['power'] / 100:>7.2f}%  "
            f"{b['ve_balance'] / 1e18:>14,.2f}  "
            f"{b['lock_end']:>10}"
        )


if __name__ == "__main__":
    main()
