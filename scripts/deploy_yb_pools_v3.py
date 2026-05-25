#!/usr/bin/env python3
"""
Deploy new YB v3 pools, a cross-pool LTMigrator, and the 5 Aragon-OSx
votes that the YB DAO needs to ratify them.

Usage:
  python deploy_yb_pools_v3.py                  # production: deploy + create votes
  python deploy_yb_pools_v3.py --test           # forked dry-run via boa.fork
  python deploy_yb_pools_v3.py --execute-votes  # call execute() on ready DAO votes
  python deploy_yb_pools_v3.py --execute-votes --test  # forked dry-run
  python deploy_yb_pools_v3.py --activate       # post-vote: initialize + seed markets
  python deploy_yb_pools_v3.py --activate --test       # forked dry-run of activation

Off-vote actions (the deployer EOA does these directly):
  1. Execute Curve Ownership-DAO vote 1408 (activates the new Twocrypto
     pool implementation). https://etherscan.io/tx/0xf0ab2beaedc45ed0daae42a544031690d86d110e3e8dd0891bf3a25ce33b97be
  2. Deploy fresh AMM + LT blueprints and the new LTMigrator.
  3. Deploy 4 bootstrap-mode Twocrypto pools (WBTC, cbBTC, tBTC, WETH)
     from TEST_EXECUTOR (tx.origin gets initialize() rights via the
     magic-gamma path).

YB DAO votes (created on Aragon-OSx via TokenVoting createProposal):
  Vote 1 — Install new AMM + LT implementations and switch the
           registered LTMigrator from the old (same-pool) contract to
           the new (cross-pool) contract.
  Vote 2 — add_market for new WBTC pool + disable old WBTC LT.
  Vote 3 — add_market for new cbBTC pool + disable old cbBTC LT.
  Vote 4 — add_market for new tBTC pool + disable old tBTC LT.
  Vote 5 — add_market for new WETH pool + disable old WETH LT.
  Vote 6 — switch HybridVaultFactory pool limits: enable the 4 new
           markets for HybridVault deposits and disable the old WETH
           market it currently points at.

Votes 2-5 are guarded by the on-chain CallComparator (same pattern as
scripts/voting/change_btc_fees_2.py): each can only execute once Vote 1 has
flipped Factory.amm_impl() / lt_impl() to the new blueprints, AND only at
an exact Factory.market_count() — which chains them to execute strictly in
deploy order (WBTC, cbBTC, tBTC, WETH) and exactly once each. Vote 6 is
likewise guarded on market_count so it cannot disable the old HybridVault
market before the 4 new markets exist.

In --test mode the script also:
  - simulates Vote 2 BEFORE Vote 1 and confirms it reverts at the guard,
  - simulates each vote in order from YB_DAO with raw_call,
  - runs the --activate path on the fork (initialize + seed),
  - tests the new LTMigrator end-to-end on a real on-chain holder.

--activate is the separate post-vote step: once Votes 2-5 have executed it
discovers the new markets on-chain, reports how much of each collateral the
activation account (yb-deployer, the pool deployer) must hold, and then
initializes each pool's LP allowlist and seeds each LT. It is idempotent.

--execute-votes is a standalone helper that calls execute() on any of the
6 DAO votes that are ready (passed quorum/duration, not yet executed). It
processes them in creation order so the per-vote market_count guards line
up. execute() is permissionless on TokenVoting, so the signing EOA only
needs gas — no proposer/admin rights required. Idempotent.
"""
import contextlib
import json
import os
import sys
import urllib.request
import warnings
from collections import namedtuple

# Silence boa's "casted bytecode does not match compiled bytecode" UserWarnings
# emitted when the on-chain Curve Twocrypto contracts were built with a slightly
# different vyper build than the local source. Behavior is unchanged.
warnings.filterwarnings(
    "ignore",
    message="casted bytecode does not match compiled bytecode.*",
    category=UserWarning,
)

import boa
import requests
from time import sleep
from eth_utils import keccak
from vyper.utils import method_id

from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK, PINATA_TOKEN, ETHERSCAN_API_KEY

VERIFY_RETRY_SECONDS = 10


# --- Curve Ownership DAO -----------------------------------------------------
CURVE_OWNERSHIP_VOTING = "0xE478de485ad2fE566d49342Cbd03E49ed7DB3356"
CURVE_OWNERSHIP_AGENT = "0x40907540d8a6c65c637785e8f8b742ae6b0b9968"
CURVE_VOTE_ID = 1408

CURVE_VOTING_ABI_PATH = os.path.join(
    os.path.dirname(__file__), "CurveAragonVoting.abi.json"
)

# Curve Twocrypto-NG factory (mainnet)
TWOCRYPTO_FACTORY = "0x98EE851a00abeE0d95D08cF4CA2BdCE32aeaAF7F"

# The pool implementation activated by vote 1408. The factory keys
# pool_implementations by uint256 (not a small slot index), so this is
# the actual key used in factory.pool_implementations[id]. Hard-coded
# rather than derived because the vote's set_pool_implementation call
# uses this exact value.
NEW_TWOCRYPTO_IMPL_ID = (
    110205523814837221872401067839670671012439480455633721548677383351514213591649
)

# --- YB DAO ------------------------------------------------------------------
YB_FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
YB_DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
YB_VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
# Gates Votes 2-5 on Vote 1 having flipped Factory.amm_impl() / lt_impl().
CALL_COMPARATOR = "0xd3BFa85dc668Aab38121bE12D69dd180301dec25"
# The on-chain LTMigrator at this address handles same-cryptopool migrations
# only. For YB v3 (each old market migrates to a *new* cryptopool) we deploy
# an upgraded LTMigrator (cross-pool aware) and register it as a limit setter.
EXISTING_LT_MIGRATOR = "0x2cdb9f485e718f551cfeea6c33cb7062ed37066c"

# HybridVaultFactory gates HybridVault deposits per market via pool_limits[id].
# Discovered on-chain via SetLimitSetter logs on the HybridFactoryOwner. Today
# only the old WETH market (CURRENT_HYBRID_POOL_ID) is enabled; Vote 6 switches
# the limits onto the 4 new markets and disables the old one.
HYBRID_VAULT_FACTORY = "0xBdC32268851C324c6185809271dfe6d8dab8dC5b"
CURRENT_HYBRID_POOL_ID = 6                  # old WETH market, the only one enabled now
HYBRID_POOL_LIMIT = 50_000_000 * 10**18     # per-market HybridVault crvUSD limit

# Largest HybridVault holder in the old WETH market — used by --activate
# --test to reproduce the production scenario where a HybridVault withdrawal
# flips disabled_lts[lt_from] back to False (which broke the legacy limit=0
# deallocation path in LTMigrator). Discovered + ranked by
# scripts/find_hybrid_weth_holder.py; the user may have withdrawn by the
# time --test runs, in which case the test silently falls back to the
# ADMIN-prank simulation.
WETH_HYBRID_VAULT_USER = "0xaaC0C7bdaB6335a925CFC9C181C1aE176B8866D7"
WETH_HYBRID_VAULT      = "0x310181E1Ffd470ac32023f2633355A44Fc24A240"

# Any address; executeVote is permissionless. Also acts as the Twocrypto
# pool deployer (tx.origin), which gives it initialize() rights via the
# magic-gamma bootstrap path.
TEST_EXECUTOR = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

AMM_IMPL_SELECTOR = method_id("amm_impl()")
LT_IMPL_SELECTOR = method_id("lt_impl()")
MARKET_COUNT_SELECTOR = method_id("market_count()")

# TokenVoting enforces a per-address proposal-creation cooldown
# (proposalCooldownPeriod = 1 day), so the 6 proposals created back-to-back
# in one run need 6 distinct proposer keys.
PROPOSER_ACCOUNT_NAMES = [
    "yb-deployer",
    "yb-deployer-a",
    "yb-deployer-b",
    "yb-deployer-c",
    "yb-deployer-2",
    "yb-deployer-3",
]

Proposal = namedtuple(
    "Proposal",
    ["metadata", "actions", "allowFailureMap", "startDate", "endDate",
     "voteOption", "tryEarlyExecution"],
)
Action = namedtuple("Action", ["to", "value", "data"])

YB_VOTING_ABI_PATH = os.path.join(
    os.path.dirname(__file__), "voting", "TokenVoting.abi.json"
)

# --- pending-vote executor (used by --activate --test) -----------------------
# Largest veYB voters; used in --activate --test to push the pending DAO votes
# created by the no-flag run through to execution so --activate can find the
# new markets. Generated by scripts/voting/find_ve_voters.py (top 25,
# ~75% of veYB power).
TEST_VOTERS = [
    "0xD10782f4D4fE20f5E15f3347E5D59aB3eC6D63d6",
    "0xb75BB762e5D62ca8f23FAbC87b1f651d0171d521",
    "0x0070D9adC687a28FBAcC0a0Aab24B90c037AD24e",
    "0x0000000C90799449af8eE0B240Da639144a36C6A",
    "0x29B6a3512FafeAce91433D278503ABC3D5aB5d12",
    "0x64A0f7Ca6efbFDaF975B5127650EC9f175182d7b",
    "0x2DE99955fA0318556f3542E71421EFD1BCF3293a",
    "0xc64566f526F09eEeFbd284760D0E1b94c6e69fc8",
    "0xb373e9C6D3Ad91eD03AA4b3C8106e68Bf3de192f",
    "0xC2059e0F6C4c1D441BAf295BC6Ea15cE24c71701",
    "0x2029Df02833eb9cd83A5143AbfE5d65BC4B46A16",
    "0xEeee8f503bCF020bc0909D35FBAbEe9ffD24641a",
    "0xaE922Cea2323fD75a31e28d10E0c84F49e1Fb6b7",
    "0xc900C3EFDC7d790f50b0B43b0c4206F8141a77eC",
    "0x52658B966801D8ddB6f49c886557084EC459Aa27",
    "0x53F8Ef5B139824C85d3f837C6b535470bFBFEeE3",
    "0xfF642740228d6abe9786612A421C0B1b859f0793",
    "0x0ca0e5c9f426d1e3EF1f568862716c68b1f44a01",
    "0x9cB95F0Fa3dB70eb15D1505d3c73a55766A1Ec89",
    "0x387ACB7f56A6f29137a21d7Eb755A3F638cab45B",
    "0x3340a1541296E7A4db2Ac6f9C3776bda7eF62724",
    "0xF791da446D04282f921f38FBF954aD5cAee899a3",
    "0x27c6DCcF18d6e66942dc5a250B1618B6B3A44036",
    "0x858a2FEf6C0833f8a5532122bb964655b0E6d5B5",
    "0x1105c075A4034C57d1457151b2D7cAb2ed99B122",
]
VOTE_YES = 2                       # Aragon TokenVoting VoteOption
RECENT_PROPOSAL_SCAN = 10          # how many of the latest proposals to look at
PROPOSAL_LOG_CHUNK = 10_000        # mainnet-cluster eth_getLogs range limit
PROPOSAL_CREATED_TOPIC0 = "0x" + keccak(
    text="ProposalCreated(uint256,address,uint64,uint64,bytes,"
         "(address,uint256,bytes)[],uint256)"
).hex()


def _rpc(method, params):
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


def _discover_our_proposals(latest_block: int, n: int,
                            creators: set[str]) -> list[int]:
    """Most-recently-created Aragon-OSx proposal ids filtered to `creators`,
    in creation order. Filtering by creator is critical: a naive
    most-recent-N scan can sweep in unrelated pending proposals whose actions
    can't be executed on the fork. Proposal ids are hashes, so they're read
    from ProposalCreated logs (creator is the indexed topic[2])."""
    creators = {c.lower() for c in creators}
    seen = {}
    hi = latest_block
    while hi > 0 and len(seen) < n:
        lo = max(hi - PROPOSAL_LOG_CHUNK + 1, 0)
        for log in _rpc("eth_getLogs", [{
            "address": YB_VOTING_PLUGIN, "topics": [PROPOSAL_CREATED_TOPIC0],
            "fromBlock": hex(lo), "toBlock": hex(hi),
        }]):
            creator = "0x" + log["topics"][2][26:]
            if creator.lower() not in creators:
                continue
            seen[int(log["topics"][1], 16)] = int(log["blockNumber"], 16)
        hi = lo - 1
    return sorted(seen, key=lambda p: seen[p])[-n:]


def _pass_vote(voting, pid: int):
    """Cast YES with the test whales until minParticipation is reached."""
    cast = 0
    for voter in TEST_VOTERS:
        if voting.isMinParticipationReached(pid):
            break
        if not voting.canVote(pid, voter, VOTE_YES):
            continue
        with boa.env.prank(voter):
            voting.vote(pid, VOTE_YES, False)
        cast += 1
    if not voting.isMinParticipationReached(pid):
        raise RuntimeError(
            f"Proposal {pid} did not reach quorum ({cast} whales voted)"
        )


def execute_ready_dao_votes() -> list[int]:
    """Production --activate helper: discover our recent Aragon-OSx proposals
    and call execute() on each that is ready (canExecute() == True and not
    already executed). Iterates in creation order so the per-vote
    CallComparator guards (market_count) are satisfied. Idempotent: anything
    already executed or not yet executable is skipped, so re-running is safe.

    execute() on TokenVoting is permissionless, so the caller just needs ETH
    for gas — no proposer/admin rights required."""
    voting = boa.load_abi(YB_VOTING_ABI_PATH, name="AragonVoting").at(
        YB_VOTING_PLUGIN
    )
    latest = int(_rpc("eth_blockNumber", []), 16)
    creators = {keystore_address(n) for n in PROPOSER_ACCOUNT_NAMES}
    recent = _discover_our_proposals(
        latest, len(PROPOSER_ACCOUNT_NAMES), creators
    )
    if not recent:
        print("No recent DAO votes from our proposer accounts.")
        return []

    print(f"\n=== Checking {len(recent)} recent DAO vote(s) for execution ===")
    executed = []
    for pid in recent:
        if voting.getProposal(pid)[1]:
            print(f"  proposal {pid} already executed - skipping")
            continue
        if not voting.canExecute(pid):
            print(f"  proposal {pid} not yet executable "
                  "(quorum/duration not met) - skipping")
            continue
        print(f"  executing proposal {pid}...")
        voting.execute(pid)
        executed.append(pid)
        print(f"  proposal {pid} executed")
    return executed


def execute_pending_dao_votes() -> list[int]:
    """Forked-test helper: push any unexecuted recent Aragon-OSx proposals
    through to execution so --activate can find the new markets. Idempotent —
    proposals already executed on-chain are skipped, so it's safe to re-run.
    Returns the proposal ids in creation order (Vote 1 first), so callers can
    inspect their actions on-chain via voting.getProposal()."""
    voting = boa.load_abi(YB_VOTING_ABI_PATH, name="AragonVoting").at(
        YB_VOTING_PLUGIN
    )
    latest = boa.env.evm.patch.block_number
    creators = {keystore_address(n) for n in PROPOSER_ACCOUNT_NAMES}
    recent = _discover_our_proposals(latest, len(PROPOSER_ACCOUNT_NAMES),
                                     creators)
    pending = [pid for pid in recent if not voting.getProposal(pid)[1]]
    if not pending:
        print("No pending DAO votes to execute on the fork.")
        return recent

    print(f"\n=== Executing {len(pending)} pending DAO vote(s) on the fork ===")
    for pid in pending:
        print(f"  voting on proposal {pid}…")
        _pass_vote(voting, pid)
    boa.env.time_travel(seconds=voting.minDuration() + 3600)
    for pid in pending:
        if voting.getProposal(pid)[1]:
            print(f"  proposal {pid} already executed (early)")
            continue
        if not voting.canExecute(pid):
            raise RuntimeError(f"Proposal {pid} cannot execute after voting")
        voting.execute(pid)
        print(f"  proposal {pid} executed")
    return recent


# --- vote execution ----------------------------------------------------------

def ensure_curve_vote_executed(vote_id: int = CURVE_VOTE_ID):
    """Execute a Curve Ownership-DAO vote on the fork, time-travelling past
    voteTime if needed. No-op if the vote is already executed."""
    voting = boa.load_abi(CURVE_VOTING_ABI_PATH, name="CurveAragonVoting").at(
        CURVE_OWNERSHIP_VOTING
    )

    vote = voting.getVote(vote_id)
    is_open, executed, start_date = vote[0], vote[1], vote[2]
    yea, nay, voting_power = vote[6], vote[7], vote[8]

    print(
        f"Curve vote {vote_id}: open={is_open} executed={executed} "
        f"yea={yea / 1e18:,.0f} nay={nay / 1e18:,.0f} "
        f"voting_power={voting_power / 1e18:,.0f}"
    )

    if executed:
        print(f"Vote {vote_id} already executed on-chain — skipping.")
        return

    vote_time = voting.voteTime()
    end_ts = start_date + vote_time
    now = boa.env.evm.patch.timestamp
    if now <= end_ts:
        delta = end_ts - now + 60
        print(f"Time-travelling +{delta}s past voteTime ({vote_time}s).")
        boa.env.time_travel(seconds=delta)

    if not voting.canExecute(vote_id):
        raise RuntimeError(
            f"Vote {vote_id} cannot execute after time-travel "
            f"(quorum/support not reached?)"
        )

    with boa.env.prank(TEST_EXECUTOR):
        voting.executeVote(vote_id)

    assert voting.getVote(vote_id)[1], "executeVote did not flip executed=True"
    print(f"Vote {vote_id} executed on the fork.")


# --- Curve pool deployment ---------------------------------------------------

# Mainnet token addresses
WBTC = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"   # 8 decimals
CBBTC = "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf"  # 8 decimals
TBTC = "0x18084fbA666a33d37592fA2633fD49a74DD93a88"   # 18 decimals
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"   # 18 decimals
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"  # 18 decimals

MAGIC_GAMMA = 11111111111  # Twocrypto bootstrap-mode gamma

# Per-pool spec for the new YB v3 pools. `initial_price` is filled in at
# runtime from CoinGecko (price of coin1 in coin0, 1e18 precision); each
# pool entry shares one of the base specs below.

# Initial add_market debt ceiling. Deliberately tiny — just enough for
# --activate to seed each market (SEED_AMOUNTS). Real capacity is NOT set
# here: LTMigrator is a registered limit setter, so each migrate_*() call
# raises lt_to's allocation to cover the migrated position and pulls the
# freed allocation off lt_from (LTMigrator.vy _migrate_plain). The cap
# only has to clear the seed deposit's amm.max_debt() // 2 >= value check.
SEED_DEBT_CAP = 2_000_000 * 10**18

# Shared cryptopool + YB params for the three BTC variants.
_BTC_BASE = {
    "coin0": CRVUSD,
    "coingecko_id": "bitcoin",
    "A": 5 * 10000,
    "mid_fee": int(0.0146 * 10**10),
    "out_fee": int(0.017 * 10**10),
    "fee_gamma": int(0.054202748 * 10**18),
    "adjustment_step_min": int(1e-10 * 10**18),
    "adjustment_step_max": int(0.005 * 10**18),
    "ma_exp_time": 865,
    "reserved_profit_fraction": int(0.301010101 * 10**10),
    "leverage_fee": int(0.013 * 10**18),
    "rate": int(0.004090909091 * 2 * 10**18 // (86400 * 365)),
    "debt_cap": SEED_DEBT_CAP,
}

# WETH pool — wider gamma + fees, longer MA. Tracks the existing on-chain
# yb-WETH market params.
_ETH_BASE = {
    "coin0": CRVUSD,
    "coingecko_id": "ethereum",
    "A": 5 * 10000,
    "mid_fee": int(0.0136 * 10**10),
    "out_fee": int(0.0282 * 10**10),
    "fee_gamma": int(0.0049619476 * 10**18),
    "adjustment_step_min": int(1e-10 * 10**18),
    "adjustment_step_max": int(0.005 * 10**18),
    "ma_exp_time": 865,
    "reserved_profit_fraction": int(0.45 * 10**10),
    "leverage_fee": int(0.013 * 10**18),
    "rate": int(0.05 * 2 * 10**18 // (86400 * 365)),
    "debt_cap": SEED_DEBT_CAP,
}

POOL_SPECS = [
    {**_BTC_BASE, "name": "Yield Basis WBTC", "symbol": "YB-WBTC",
     "coin1": WBTC, "replaces_market_id": 3},
    {**_BTC_BASE, "name": "Yield Basis cbBTC", "symbol": "YB-cbBTC",
     "coin1": CBBTC, "replaces_market_id": 4},
    {**_BTC_BASE, "name": "Yield Basis tBTC", "symbol": "YB-tBTC",
     "coin1": TBTC, "replaces_market_id": 5},
    {**_ETH_BASE, "name": "Yield Basis WETH", "symbol": "YB-WETH",
     "coin1": WETH, "replaces_market_id": 6},
]

# Collateral, in human units, that --activate seeds into each new market.
# The activation account must hold these before running --activate.
SEED_AMOUNTS = {
    "YB-WBTC": 0.001,
    "YB-cbBTC": 0.001,
    "YB-tBTC": 0.001,
    "YB-WETH": 0.01,
}


COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_MAX_RETRIES = 6
COINGECKO_RETRY_SECONDS = 15

# In-memory cache: each coingecko_id is fetched at most once per run.
_PRICE_CACHE: dict[str, int] = {}


def fetch_initial_price(coingecko_id: str) -> int:
    """Fetch coin1 USD spot from CoinGecko and return it scaled to 1e18
    (price of coin1 in coin0 = crvUSD, treated as 1 USD). Cached per
    coingecko_id for the lifetime of the run; retries on 429 rate-limit /
    5xx, since the free tier throttles bursts of requests."""
    if coingecko_id in _PRICE_CACHE:
        price = _PRICE_CACHE[coingecko_id]
        print(f"CoinGecko {coingecko_id} = ${price / 1e18:,.2f} (cached)")
        return price
    for attempt in range(1, COINGECKO_MAX_RETRIES + 1):
        response = requests.get(
            COINGECKO_PRICE_URL,
            params={"ids": coingecko_id, "vs_currencies": "usd"},
            timeout=15,
        )
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == COINGECKO_MAX_RETRIES:
                response.raise_for_status()
            print(
                f"CoinGecko {response.status_code} for {coingecko_id} — "
                f"retry {attempt}/{COINGECKO_MAX_RETRIES} in "
                f"{COINGECKO_RETRY_SECONDS}s"
            )
            sleep(COINGECKO_RETRY_SECONDS)
            continue
        response.raise_for_status()
        price_usd = float(response.json()[coingecko_id]["usd"])
        print(f"CoinGecko {coingecko_id} = ${price_usd:,.2f}")
        price = int(price_usd * 10**18)
        _PRICE_CACHE[coingecko_id] = price
        return price


def deploy_curve_pool(twocrypto_factory, spec: dict) -> str:
    with boa.env.prank(TEST_EXECUTOR):
        pool_addr = twocrypto_factory.deploy_pool(
            spec["name"],
            spec["symbol"],
            [spec["coin0"], spec["coin1"]],
            NEW_TWOCRYPTO_IMPL_ID,
            spec["A"],
            MAGIC_GAMMA,
            spec["mid_fee"],
            spec["out_fee"],
            spec["fee_gamma"],
            spec["adjustment_step_min"],
            spec["adjustment_step_max"],
            spec["ma_exp_time"],
            spec["initial_price"],
        )
    print(f"Deployed Twocrypto pool {spec['symbol']}: {pool_addr}")
    return pool_addr


# --- YB Aragon-OSx vote builders + simulator ---------------------------------

def addr_as_uint256(addr: str) -> int:
    """Address -> uint256. CallComparator.check_equal compares a uint256, and
    `amm_impl()` / `lt_impl()` return the raw 32-byte address word; matching
    them in Python means parsing the hex address as an int."""
    return int(addr, 16)


@contextlib.contextmanager
def measure_gas(label: str):
    """Reset boa's gas tracker, run the wrapped block, then print the EVM gas
    consumed inside it. Used to size votes against on-chain gas limits."""
    boa.env.reset_gas_used()
    try:
        yield
    finally:
        print(f"  [GAS] {label}: {boa.env.get_gas_used():,} gas")


def simulate_yb_vote(actions: list, label: str = ""):
    """Simulate a YB Aragon-OSx vote by raw_calling each action from the DAO.
    Matches what the voting plugin does on executeVote."""
    tag = f" ({label})" if label else ""
    print(f"Simulating YB vote{tag} with {len(actions)} action(s) from {YB_DAO}…")
    with boa.env.prank(YB_DAO), measure_gas(f"vote{tag}"):
        for action in actions:
            boa.env.raw_call(to_address=action.to, data=action.data)
    print(f"YB vote{tag} actions executed.")


# --- Aragon proposer accounts ------------------------------------------------

def keystore_address(name: str) -> str:
    """Read the EOA address from a brownie keystore file. The address is
    stored in plaintext alongside the encrypted key, so this does NOT prompt
    for the password — used in fork mode to prank-create proposals."""
    path = os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", name + ".json")
    )
    with open(path) as f:
        return "0x" + json.load(f)["address"]


# --- IPFS metadata pinning (production proposals) ----------------------------

def pin_to_ipfs(content: dict) -> str:
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "Authorization": f"Bearer {PINATA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "pinataContent": content,
        "pinataMetadata": {"name": "pinnie.json"},
        "pinataOptions": {"cidVersion": 1},
    }
    response = requests.post(url, json=payload, headers=headers, timeout=30)
    assert 200 <= response.status_code < 400, response.text
    return "ipfs://" + response.json()["IpfsHash"]


# --- bootstrap initial liquidity --------------------------------------------

ERC20_ABI_PATH = os.path.join(os.path.dirname(__file__), "erc20.abi.json")


def _load_erc20(addr: str):
    """Load an ERC20 via ABI only — avoids Vyper bytecode-cast warning
    paths that crash on non-UTF8 storage in some Solidity contracts (cbBTC)."""
    return boa.load_abi(ERC20_ABI_PATH, name="ERC20").at(addr)


WETH_DEPOSIT_ABI = [{
    "name": "deposit", "inputs": [], "outputs": [],
    "stateMutability": "payable", "type": "function",
}]


def _give_asset(asset, account: str, amount: int):
    """Mint `amount` of `asset` to `account` on the fork.
    Uses boa.deal for normal ERC20s; falls back to WETH.deposit() for WETH,
    whose totalSupply is computed from the contract's ETH balance."""
    if asset.address.lower() == WETH.lower():
        weth_deposit = boa.loads_abi(
            __import__("json").dumps(WETH_DEPOSIT_ABI), name="WETH9"
        ).at(asset.address)
        boa.env.set_balance(account, boa.env.get_balance(account) + amount)
        with boa.env.prank(account):
            weth_deposit.deposit(value=amount)
        return
    boa.deal(asset, account, amount)


def leverage_deposit(lt, asset, cryptopool, seed_assets: int) -> int:
    """Approve and leverage-deposit `seed_assets` of `asset` into `lt` from the
    current EOA, which must already hold the assets. Returns LT shares minted.
    The matching crvUSD debt is borrowed from the factory allocation, so the
    EOA only needs the collateral asset itself."""
    decimals = asset.decimals()
    debt = seed_assets * cryptopool.price_oracle() // 10**decimals
    asset.approve(lt.address, 2**256 - 1)
    before = lt.totalSupply()
    lt.deposit(seed_assets, debt, 0, boa.env.eoa)
    return lt.totalSupply() - before


# --- LTMigrator test ---------------------------------------------------------

def _find_lt_holder(lt_addr: str) -> str:
    """Return the SMALLEST non-gauge address with a real direct LT balance.
    Picking the smallest keeps the migration test cheap to seed against."""
    r = requests.get(
        f"https://api.ethplorer.io/getTopTokenHolders/{lt_addr}",
        params={"apiKey": "freekey", "limit": 10},
        timeout=20,
    )
    r.raise_for_status()
    candidates = [
        h for h in r.json().get("holders", [])
        if h["address"].lower() != GAUGE_HOLDERS_TO_SKIP.lower()
        and int(h["balance"]) > 0
    ]
    if not candidates:
        raise RuntimeError(f"No direct LT holder found for {lt_addr}")
    return min(candidates, key=lambda h: int(h["balance"]))["address"]


def test_lt_migration(factory, lt_interface, spec: dict, new_market_id: int,
                      holder: str, balance: int, migrator):
    old_market_id = spec["replaces_market_id"]
    old_lt = lt_interface.at(factory.markets(old_market_id).lt)
    new_lt = lt_interface.at(factory.markets(new_market_id).lt)
    print(
        f"\n=== Testing LTMigrator @ {migrator.address}: "
        f"market #{old_market_id} ({old_lt.symbol()}) -> "
        f"#{new_market_id} ({new_lt.symbol()}) ==="
    )
    print(f"  holder={holder} balance={balance / 1e18} {old_lt.symbol()}")

    received_before = new_lt.balanceOf(holder)
    with boa.env.prank(holder):
        old_lt.approve(migrator.address, 2**256 - 1)
        preview = migrator.preview_migrate_plain(
            old_lt.address, new_lt.address, balance
        )
        print(f"  preview_migrate_plain: {preview / 1e18}")
        try:
            migrator.migrate_plain(
                old_lt.address,
                new_lt.address,
                balance,
                int(preview * 0.998),
            )
        except Exception as e:
            print(f"  ✗ migrate_plain reverted: {e}")
            raise

    received = new_lt.balanceOf(holder) - received_before
    print(f"  ✓ Received {received / 1e18} {new_lt.symbol()}")
    assert received > 0, "migrate_plain returned 0 shares"


GAUGE_HOLDERS_TO_SKIP = "0x0000000000000000000000000000000000000000"

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# --- implementation refresh -------------------------------------------------
#
# Verified manually: AMM + LT bytecode have diverged from on-chain, but
# VirtualPool still matches the deployed blueprint — so v3 only refreshes
# AMM + LT and leaves VirtualPool / price_oracle / staker untouched.

def verify_on_etherscan(contract, etherscan):
    """Submit `contract` for Etherscan verification, retrying on transient errors."""
    while True:
        try:
            sleep(VERIFY_RETRY_SECONDS)
            boa_verify(contract, etherscan, wait=True)
            return
        except ValueError as e:
            print(e)
            if "Already Verified" in str(e):
                return


def deploy_new_implementations(etherscan=None):
    """Deploy fresh AMM + LT blueprints. If `etherscan` is provided, each
    blueprint is also submitted to Etherscan for source verification."""
    amm_bp = boa.load_partial("contracts/AMM.vy").deploy_as_blueprint()
    lt_bp = boa.load_partial("contracts/LT.vy").deploy_as_blueprint()
    print(f"  New AMM blueprint: {amm_bp.address}")
    print(f"  New LT  blueprint: {lt_bp.address}")
    if etherscan is not None:
        # Blueprints have no constructor args, but boa needs ctor_calldata
        # set explicitly for the verifier to know that.
        amm_bp.ctor_calldata = b""
        lt_bp.ctor_calldata = b""
        verify_on_etherscan(amm_bp, etherscan)
        verify_on_etherscan(lt_bp, etherscan)
    return amm_bp, lt_bp


def deploy_new_migrator(yb_factory, factory_owner, etherscan=None):
    migrator = boa.load(
        "contracts/LTMigrator.vy",
        yb_factory.STABLECOIN(),
        factory_owner.address,
    )
    print(f"Deployed new LTMigrator: {migrator.address}")
    if etherscan is not None:
        verify_on_etherscan(migrator, etherscan)
    return migrator


# --- pool initialization (whitelist) -----------------------------------------

def initialize_pool(pool, allowlist: list[str], reserved_profit_fraction: int,
                    initial_price: int):
    """Take a bootstrap-mode Twocrypto pool out of init mode: set fee params
    and the LP allowlist. The current EOA must be the pool's deploy_eoa."""
    admin_fee = 0                     # LPs get the full admin share
    pool.initialize(
        reserved_profit_fraction,
        admin_fee,
        ZERO_ADDRESS,                 # policy
        initial_price,
        allowlist,
    )
    print(f"  initialized pool {pool.address} allowlist={allowlist}")


# --- post-vote activation ----------------------------------------------------

def discover_new_markets(yb_factory, pool_interface) -> dict:
    """Map each POOL_SPECS symbol to its on-chain YB market id. A market is
    matched by its cryptopool's collateral (coins[1]); only markets newer than
    the replaced ones are considered. Raises if any market is missing."""
    replaced_ids = {s["replaces_market_id"] for s in POOL_SPECS}
    first_new_id = max(replaced_ids) + 1
    count = yb_factory.market_count()

    found = {}
    for mid in range(count - 1, first_new_id - 1, -1):
        cryptopool = pool_interface.at(yb_factory.markets(mid).cryptopool)
        coin1 = cryptopool.coins(1)
        for spec in POOL_SPECS:
            if spec["symbol"] not in found and \
                    coin1.lower() == spec["coin1"].lower():
                found[spec["symbol"]] = mid
                break

    missing = [s["symbol"] for s in POOL_SPECS if s["symbol"] not in found]
    if missing:
        raise RuntimeError(
            f"No new market found for {missing} — have Votes 2-5 executed?"
        )
    return found


def run_activation(yb_factory, pool_interface, lt_interface) -> bool:
    """Discover the new markets, report the collateral the current EOA must
    hold, and — if it is funded — initialize each pool and seed each LT.
    Idempotent: pools/LTs already activated are skipped. Returns False (and
    changes nothing) if the account is underfunded."""
    account = boa.env.eoa
    markets = discover_new_markets(yb_factory, pool_interface)

    print(f"\n=== Activating {len(markets)} markets as {account} ===")
    plan = []
    for spec in POOL_SPECS:
        symbol = spec["symbol"]
        mid = markets[symbol]
        asset = _load_erc20(spec["coin1"])
        decimals = asset.decimals()
        seed_raw = int(round(SEED_AMOUNTS[symbol] * 10**decimals))
        plan.append({"spec": spec, "mid": mid, "asset": asset,
                     "decimals": decimals, "seed_raw": seed_raw})
        print(f"  {symbol:9s} market #{mid}")

    print(f"\nCollateral required from {account}:")
    underfunded = False
    for item in plan:
        asset, decimals, seed_raw = item["asset"], item["decimals"], item["seed_raw"]
        balance = asset.balanceOf(account)
        have, need = balance / 10**decimals, seed_raw / 10**decimals
        token = asset.symbol()
        if balance < seed_raw:
            underfunded = True
            short = (seed_raw - balance) / 10**decimals
            print(f"  {token:6s} have {have:.8f}  need {need:.8f}  "
                  f"TRANSFER {short:.8f}")
        else:
            print(f"  {token:6s} have {have:.8f}  need {need:.8f}  OK")
    if underfunded:
        print("\nUnderfunded — aborting. Fund the account and re-run.")
        return False

    for item in plan:
        spec, mid = item["spec"], item["mid"]
        market = yb_factory.markets(mid)
        pool = pool_interface.at(market.cryptopool)
        lt = lt_interface.at(market.lt)
        print(f"\n--- {spec['symbol']} (market #{mid}) ---")

        if pool.lp_allowlist(market.lt):
            print("  pool already initialized — skipping")
        else:
            initial_price = fetch_initial_price(spec["coingecko_id"])
            initialize_pool(
                pool, [account, market.lt, market.virtual_pool],
                spec["reserved_profit_fraction"], initial_price,
            )

        if lt.totalSupply() > 0:
            print(f"  LT already seeded (totalSupply={lt.totalSupply() / 1e18}) "
                  "— skipping")
        else:
            shares = leverage_deposit(lt, item["asset"], pool, item["seed_raw"])
            print(f"  seeded {item['seed_raw'] / 10**item['decimals']} "
                  f"{item['asset'].symbol()} -> {shares / 1e18} LT shares")
            assert shares > 0, "seed deposit produced 0 LT shares"

    print("\n=== Activation complete ===")
    return True


# --- entrypoint --------------------------------------------------------------

def _account_load(fname: str):
    from eth_account import account
    from getpass import getpass
    key_path = os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", fname + ".json")
    )
    with open(key_path, "r") as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
    return account.Account.from_key(pkey)


def run_activate_mode():
    """`--activate`: post-vote activation. Runs after Votes 2-5 have executed
    on-chain — discovers the new markets, reports the collateral the activation
    account must hold, and initializes + seeds each market. The network env is
    set up by main() before this is called; `--activate --test` forks for a
    dry-run (real balances, no real txs)."""
    yb_factory = boa.load_partial("contracts/Factory.vy").at(YB_FACTORY)
    pool_interface = boa.load_partial(
        "contracts/twocrypto_pool/contracts/main/Twocrypto.vy"
    )
    lt_interface = boa.load_partial("contracts/LT.vy")
    try:
        run_activation(yb_factory, pool_interface, lt_interface)
    except RuntimeError as e:
        sys.exit(f"Activation aborted: {e}")


SET_LIMIT_SETTER_SELECTOR = method_id("set_limit_setter(address,bool)")

# v3 cross-pool LTMigrator deployed and registered (as limit_setter) by Vote 1.
# Hard-coded here so the LT-migration test doesn't depend on Vote 1 still being
# in the recent-proposal window (the n=6 cap in _discover_our_proposals can
# age it out once unrelated proposals are created from the same accounts).
V3_LT_MIGRATOR = "0xDfD6fe3A540F68601002E889E33117a7E8A0669D"


def _try_hybridvault_withdraw(yb_factory, factory_owner, lt_interface,
                              old_id: int) -> bool:
    """If WETH_HYBRID_VAULT_USER still holds a position in market `old_id`,
    prank them to withdraw a quarter via HybridVault.withdraw() — the
    production re-enable trigger that breaks the legacy limit=0 deallocation
    path. Returns True if the withdrawal happened (disabled_lts now False)."""
    market = yb_factory.markets(old_id)
    old_lt = lt_interface.at(market.lt)
    hybrid_vault = boa.load_partial("contracts/HybridVault.vy").at(
        WETH_HYBRID_VAULT)
    staker = boa.load_partial("contracts/dao/LiquidityGauge.vy").at(
        market.staker)
    gauge_bal = staker.balanceOf(WETH_HYBRID_VAULT)
    lt_bal = old_lt.balanceOf(WETH_HYBRID_VAULT)
    if gauge_bal == 0 and lt_bal == 0:
        print(f"  HybridVault {WETH_HYBRID_VAULT} no longer holds WETH LT "
              "- skipping the real re-enable trigger.")
        return False
    unstake = gauge_bal > 0
    shares = (gauge_bal // 4) if unstake else (lt_bal // 4)
    assert shares > 0, "computed 0 withdrawal shares"
    with boa.env.prank(WETH_HYBRID_VAULT_USER):
        hybrid_vault.withdraw(old_id, shares, 0, unstake)
    assert not factory_owner.disabled_lts(old_lt.address), (
        "HybridVault withdrawal did not flip disabled_lts back to False"
    )
    print(f"  HybridVault withdrew {shares / 1e18:.4f} WETH LT shares "
          f"({'unstaked' if unstake else 'direct'}) — disabled_lts cleared")
    return True


def run_lt_migration_test(withdraw_hybrid: bool,
                          use_new_migrator: bool):
    """Forked-test: exercise LTMigrator end-to-end on each market.

    Migrator selection (the key flag):
      default          - call the ON-CHAIN migrator (the address Vote 1 set
                         as limit_setter). This is the build with the legacy
                         lt_allocate_stablecoins(lt_from, 0) call.
      --new-migrator   - deploy a fresh local LTMigrator (current source)
                         and register it via ADMIN prank.

    Re-enable selection:
      default            - leave disabled_lts[lt_from] = True. Both the
                           on-chain and the fresh migrator take the legacy
                           limit=0 path and succeed.
      --withdraw-hybrid  - for the WETH market only: prank
                           WETH_HYBRID_VAULT_USER to withdraw from
                           WETH_HYBRID_VAULT, the production trigger that
                           flips disabled_lts[lt_from] back to False. Now:
                             * the on-chain (default) migrator REVERTS with
                               'Not disabled' - bug demonstrated;
                             * --new-migrator takes the limit=1 fallback,
                               leaves lt_from at sentinel allocation=1,
                               and refuses a migrate-back into lt_from.

    BTC markets are never re-enabled (no HybridVault holds them), so for
    them both migrators simply succeed via the legacy limit=0 path."""
    yb_factory = boa.load_partial("contracts/Factory.vy").at(YB_FACTORY)
    factory_owner = boa.load_partial("contracts/HybridFactoryOwner.vy").at(
        yb_factory.admin())
    pool_interface = boa.load_partial(
        "contracts/twocrypto_pool/contracts/main/Twocrypto.vy")
    lt_interface = boa.load_partial("contracts/LT.vy")

    if use_new_migrator:
        migrator = boa.load("contracts/LTMigrator.vy",
                            yb_factory.STABLECOIN(), factory_owner.address)
        with boa.env.prank(factory_owner.ADMIN()):
            factory_owner.set_limit_setter(migrator.address, True)
        print(f"\n=== LTMigrator test - FRESH local migrator "
              f"{migrator.address} ===")
    else:
        migrator = boa.load_partial(
            "contracts/LTMigrator.vy").at(V3_LT_MIGRATOR)
        print(f"\n=== LTMigrator test - ON-CHAIN migrator "
              f"{V3_LT_MIGRATOR} (Vote 1 registered) ===")

    new_markets = discover_new_markets(yb_factory, pool_interface)
    global GAUGE_HOLDERS_TO_SKIP
    for spec in POOL_SPECS:
        old_id = spec["replaces_market_id"]
        new_id = new_markets[spec["symbol"]]
        old_lt = lt_interface.at(yb_factory.markets(old_id).lt)
        new_lt = lt_interface.at(yb_factory.markets(new_id).lt)

        print(f"\n--- {spec['symbol']} (market #{old_id} -> #{new_id}) ---")

        re_enabled = False
        if withdraw_hybrid and spec["symbol"] == "YB-WETH":
            re_enabled = _try_hybridvault_withdraw(
                yb_factory, factory_owner, lt_interface, old_id)

        GAUGE_HOLDERS_TO_SKIP = yb_factory.markets(old_id).staker
        holder = _find_lt_holder(old_lt.address)
        balance = old_lt.balanceOf(holder)

        # On-chain (buggy) migrator + re-enabled lt_from -> expected revert.
        if re_enabled and not use_new_migrator:
            print("  expecting on-chain migrator to REVERT (bug demo)...")
            reverted = False
            with boa.env.prank(holder):
                old_lt.approve(migrator.address, 2**256 - 1)
                try:
                    migrator.migrate_plain(
                        old_lt.address, new_lt.address, balance, 0)
                except Exception as e:
                    reverted = True
                    print(f"  reverted: "
                          f"{str(e).splitlines()[-1][:120]}")
            if not reverted:
                raise AssertionError(
                    f"{spec['symbol']}: on-chain migrator did NOT revert "
                    "after HybridVault re-enabled disabled_lts - fix is "
                    "no longer needed?")
            continue

        test_lt_migration(
            yb_factory, lt_interface, spec, new_id, holder, balance, migrator,
        )

        if use_new_migrator and re_enabled:
            assert old_lt.stablecoin_allocation() == 1, (
                f"{spec['symbol']}: lt_from allocation="
                f"{old_lt.stablecoin_allocation()}, expected sentinel 1")
            print("  lt_from soft-deprecated (allocation=1)")

        # migrate-back must be refused. The on-chain migrator catches it via
        # the disabled_lts guard (still True after the legacy limit=0 path);
        # the new migrator catches it via either guard.
        try:
            migrator.preview_migrate_plain(new_lt.address, old_lt.address, 1)
            raise AssertionError(
                f"{spec['symbol']}: preview accepted migrate-back")
        except Exception as e:
            if "lt_to deprecated" not in str(e):
                raise
            print("  migrate-back correctly rejected")


def main():
    """`--test`            — forked dry-run of the full deploy + 6-vote flow.
    `--execute-votes`     — call execute() on ready DAO votes (idempotent).
    `--execute-votes --test` — forked dry-run of vote execution.
    `--activate`          — post-vote activation (initialize + seed the new markets).
    `--activate --test`   — forked dry-run of activation.
    no flags              — production: deploy, verify, and create the 6 votes."""
    test_mode = "--test" in sys.argv[1:]
    activate_mode = "--activate" in sys.argv[1:]
    execute_votes_mode = "--execute-votes" in sys.argv[1:]
    withdraw_hybrid = "--withdraw-hybrid" in sys.argv[1:]
    use_new_migrator = "--new-migrator" in sys.argv[1:]

    eoa_account_modes = activate_mode or execute_votes_mode

    # --- network env -------------------------------------------------------
    # Initial signer = first proposer; covers all pre-vote deployments
    # (blueprints, migrator, Curve pools) and post-vote activation. The
    # createProposal loop below switches the active EOA per vote.
    if test_mode:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = (
            keystore_address(PROPOSER_ACCOUNT_NAMES[0])
            if eoa_account_modes else TEST_EXECUTOR
        )
        etherscan = None
    else:
        boa.set_network_env(NETWORK)
        boa.env.add_account(
            _account_load(PROPOSER_ACCOUNT_NAMES[0]), force_eoa=True
        )
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    if execute_votes_mode:
        # On the fork the votes haven't been ratified yet — push them through
        # whales/time-travel first; in production canExecute() already gates
        # which ones are ready to land.
        if test_mode:
            execute_pending_dao_votes()
        else:
            execute_ready_dao_votes()
        return

    if activate_mode:
        # In --test mode the new markets haven't been ratified on the fork yet;
        # push any pending DAO votes through before discovery so run_activation
        # finds them, and mint the per-market seed collateral to the activation
        # EOA so run_activation isn't underfunded. Both no-ops are harmless on a
        # fork where the votes are already executed / the account is funded.
        if test_mode:
            execute_pending_dao_votes()
            for spec in POOL_SPECS:
                asset = _load_erc20(spec["coin1"])
                raw = int(round(
                    SEED_AMOUNTS[spec["symbol"]] * 10**asset.decimals()))
                _give_asset(asset, boa.env.eoa, raw)
        run_activate_mode()
        if test_mode:
            run_lt_migration_test(withdraw_hybrid, use_new_migrator)
        return

    # --- shared contract handles ------------------------------------------
    twocrypto_factory = boa.load_partial(
        "contracts/twocrypto_pool/contracts/main/TwocryptoFactory.vy"
    ).at(TWOCRYPTO_FACTORY)
    pool_interface = boa.load_partial(
        "contracts/twocrypto_pool/contracts/main/Twocrypto.vy"
    )
    yb_factory = boa.load_partial("contracts/Factory.vy").at(YB_FACTORY)
    lt_interface = boa.load_partial("contracts/LT.vy")
    factory_owner = boa.load_partial("contracts/HybridFactoryOwner.vy").at(
        yb_factory.admin()
    )
    comparator = boa.load_partial("contracts/dao/CallComparator.vy").at(
        CALL_COMPARATOR
    )
    hybrid_vault_factory = boa.load_partial(
        "contracts/HybridVaultFactory.vy"
    ).at(HYBRID_VAULT_FACTORY)
    voting = boa.load_abi(YB_VOTING_ABI_PATH, name="AragonVoting").at(
        YB_VOTING_PLUGIN
    )
    assert factory_owner.ADMIN() == YB_DAO, (
        f"FactoryOwner admin is {factory_owner.ADMIN()}, expected YB DAO {YB_DAO}"
    )

    # --- TEST-ONLY: make sure the new Curve impl is active on the fork ----
    if test_mode:
        ensure_curve_vote_executed(CURVE_VOTE_ID)
        activated = twocrypto_factory.pool_implementations(
            NEW_TWOCRYPTO_IMPL_ID
        )
        print(f"Activated pool implementation: {activated}")
        assert activated != ZERO_ADDRESS, (
            "Implementation slot is empty after vote 1408 — wrong id?"
        )
        boa.env.set_balance(TEST_EXECUTOR, 100 * 10**18)

    # --- Deploy new blueprints + LTMigrator (pre-vote, off-chain) ---------
    print("\n=== Deploying new AMM + LT blueprints ===")
    amm_bp, lt_bp = deploy_new_implementations(etherscan=etherscan)
    migrator = deploy_new_migrator(yb_factory, factory_owner, etherscan=etherscan)

    # --- Vote 1: install new AMM + LT impls + switch registered migrator.
    # VirtualPool / PriceOracle / Staker are passed empty so the factory
    # leaves them as-is.
    vote1 = {
        "title": "YB v3: install new AMM + LT implementations and migrator",
        "summary": (
            f"Install new AMM blueprint {amm_bp.address} and LT blueprint "
            f"{lt_bp.address}; register new LTMigrator {migrator.address} "
            f"and unregister old LTMigrator {EXISTING_LT_MIGRATOR}."
        ),
        "actions": [
            Action(to=factory_owner.address, value=0,
                   data=factory_owner.set_implementations.prepare_calldata(
                       amm_bp.address, lt_bp.address,
                       ZERO_ADDRESS, ZERO_ADDRESS, ZERO_ADDRESS)),
            Action(to=factory_owner.address, value=0,
                   data=factory_owner.set_limit_setter.prepare_calldata(
                       migrator.address, True)),
            Action(to=factory_owner.address, value=0,
                   data=factory_owner.set_limit_setter.prepare_calldata(
                       EXISTING_LT_MIGRATOR, False)),
        ],
    }

    # --- Votes 2-5: per-market add_market + disable old LT.
    # Three comparator guards per vote:
    #   * check_equal(Factory.amm_impl / lt_impl) — reverts unless Vote 1 has
    #     installed the new blueprints;
    #   * check_equal(Factory.market_count) — pins each vote to an exact
    #     market_count. add_market increments it by 1, so this both forces
    #     Votes 2-5 to execute in deploy order (WBTC, cbBTC, tBTC, WETH) and
    #     prevents any of them executing twice.
    n_markets = yb_factory.market_count()
    market_votes = []
    for idx, spec in enumerate(POOL_SPECS):
        print(
            f"\n=== Deploying Curve pool for market replacing "
            f"#{spec['replaces_market_id']} ==="
        )
        spec["initial_price"] = fetch_initial_price(spec["coingecko_id"])
        pool_addr = deploy_curve_pool(twocrypto_factory, spec)
        old_lt_addr = yb_factory.markets(spec["replaces_market_id"]).lt
        market_votes.append({
            "spec": spec,
            "title": f"YB v3: add {spec['symbol']} market",
            "summary": (
                f"Add YB market on new {spec['symbol']} Curve pool "
                f"({pool_addr}) and disable old LT {old_lt_addr}. "
                "Guarded: only executes once Vote 1 has installed the new "
                f"AMM + LT implementations and market_count == {n_markets + idx} "
                "(i.e. the preceding market votes have executed)."
            ),
            "actions": [
                Action(to=comparator.address, value=0,
                       data=comparator.check_equal.prepare_calldata(
                           yb_factory.address, AMM_IMPL_SELECTOR,
                           addr_as_uint256(amm_bp.address))),
                Action(to=comparator.address, value=0,
                       data=comparator.check_equal.prepare_calldata(
                           yb_factory.address, LT_IMPL_SELECTOR,
                           addr_as_uint256(lt_bp.address))),
                Action(to=comparator.address, value=0,
                       data=comparator.check_equal.prepare_calldata(
                           yb_factory.address, MARKET_COUNT_SELECTOR,
                           n_markets + idx)),
                Action(to=factory_owner.address, value=0,
                       data=factory_owner.add_market.prepare_calldata(
                           pool_addr, spec["leverage_fee"], spec["rate"],
                           spec["debt_cap"])),
                Action(to=factory_owner.address, value=0,
                       data=factory_owner.lt_allocate_stablecoins.prepare_calldata(
                           old_lt_addr, 0)),
            ],
        })

    # --- Vote 6: switch HybridVault to the new markets.
    # HybridVaultFactory.pool_limits[id] caps HybridVault deposits per market;
    # an id with limit 0 is effectively unsupported. Today only the old WETH
    # market is enabled, so this vote enables the 4 new markets and disables
    # the old one. Guarded on market_count == n_markets + 4 so it cannot
    # disable the old market before the new ones have been created.
    enabled_old = [
        i for i in range(n_markets)
        if hybrid_vault_factory.pool_limits(i) > 0
    ]
    assert enabled_old == [CURRENT_HYBRID_POOL_ID], (
        f"HybridVault has markets {enabled_old} enabled, expected only "
        f"[{CURRENT_HYBRID_POOL_ID}] — review Vote 6 before deploying."
    )
    new_market_ids = [n_markets + i for i in range(len(POOL_SPECS))]
    vote6 = {
        "title": "YB v3: switch HybridVault to the new markets",
        "summary": (
            f"Set HybridVault deposit limits to "
            f"{HYBRID_POOL_LIMIT // 10**18:,} crvUSD for the 4 new markets "
            f"{new_market_ids} and disable the old WETH market "
            f"#{CURRENT_HYBRID_POOL_ID}. Guarded: only executes once "
            f"market_count == {n_markets + len(POOL_SPECS)} (Votes 2-5 done)."
        ),
        "actions": [
            Action(to=comparator.address, value=0,
                   data=comparator.check_equal.prepare_calldata(
                       yb_factory.address, MARKET_COUNT_SELECTOR,
                       n_markets + len(POOL_SPECS))),
        ] + [
            Action(to=hybrid_vault_factory.address, value=0,
                   data=hybrid_vault_factory.set_pool_limit.prepare_calldata(
                       market_id, HYBRID_POOL_LIMIT))
            for market_id in new_market_ids
        ] + [
            Action(to=hybrid_vault_factory.address, value=0,
                   data=hybrid_vault_factory.set_pool_limit.prepare_calldata(
                       CURRENT_HYBRID_POOL_ID, 0)),
        ],
    }

    all_votes = [vote1] + market_votes + [vote6]

    # --- TEST-ONLY: market vote must revert before Vote 1 executes --------
    if test_mode:
        print("\n=== Negative test: Vote 2 (WBTC) before Vote 1 ===")
        try:
            with boa.env.prank(YB_DAO):
                for action in market_votes[0]["actions"]:
                    boa.env.raw_call(to_address=action.to, data=action.data)
            raise AssertionError(
                "Vote 2 simulated successfully BEFORE Vote 1 — guard is broken."
            )
        except Exception as e:
            print(f"  Correctly reverted: {e}")

        print("\n=== Negative test: Vote 6 before the market votes ===")
        try:
            with boa.env.prank(YB_DAO):
                for action in vote6["actions"]:
                    boa.env.raw_call(to_address=action.to, data=action.data)
            raise AssertionError(
                "Vote 6 simulated successfully BEFORE the market votes — "
                "guard is broken."
            )
        except Exception as e:
            print(f"  Correctly reverted: {e}")

    # --- Checkpoint veYB before creating votes (VotingEscrow workaround) --
    print("\n=== Checkpointing veYB (VotingEscrow) before creating votes ===")
    ve_yb = boa.load_partial("contracts/dao/VotingEscrow.vy").at(
        voting.getVotingToken()
    )
    ve_yb.checkpoint()
    print(f"  checkpoint() called on veYB {ve_yb.address}")

    # --- Create each Aragon proposal, rotating proposer EOAs --------------
    print(f"\n=== Creating {len(all_votes)} Aragon proposals ===")
    for idx, (acct_name, vote) in enumerate(
        zip(PROPOSER_ACCOUNT_NAMES, all_votes), start=1
    ):
        if test_mode:
            proposer = keystore_address(acct_name)
            metadata = b""
        else:
            print(f"\n--- Loading creator account {acct_name} for Vote {idx} ---")
            account = _account_load(acct_name)
            # Switch the signing EOA to this proposer. Each vote MUST be
            # created from its own key — TokenVoting's per-address 1-day
            # cooldown rejects a second proposal from the same creator.
            # set_eoa() == add_account(account, force_eoa=True).
            boa.env.set_eoa(account)
            proposer = account.address
            assert boa.env.eoa == proposer, (
                f"EOA switch failed: env.eoa={boa.env.eoa}, expected {proposer}"
            )
            print(f"  signing EOA switched to {proposer}")
            metadata = pin_to_ipfs({
                "title": vote["title"],
                "summary": vote["summary"],
                "resources": [],
            }).encode()

        if test_mode:
            with boa.env.prank(proposer):
                proposal_id = voting.createProposal(*Proposal(
                    metadata=metadata,
                    actions=vote["actions"],
                    allowFailureMap=0,
                    startDate=0,
                    endDate=0,
                    voteOption=0,
                    tryEarlyExecution=True,
                ))
        else:
            proposal_id = voting.createProposal(*Proposal(
                metadata=metadata,
                actions=vote["actions"],
                allowFailureMap=0,
                startDate=0,
                endDate=0,
                voteOption=0,
                tryEarlyExecution=True,
            ))
        print(f"Vote {idx} from {acct_name} ({proposer}): proposalId={proposal_id}")

    if not test_mode:
        return

    # --- TEST-ONLY: simulate each vote's actions from the DAO -------------
    print("\n=== Simulating Vote 1 (install impls + switch migrator) ===")
    simulate_yb_vote(vote1["actions"], label="Vote 1")
    assert yb_factory.amm_impl() == amm_bp.address
    assert yb_factory.lt_impl() == lt_bp.address
    assert factory_owner.limit_setters(migrator.address) is True
    assert factory_owner.limit_setters(EXISTING_LT_MIGRATOR) is False
    print("  Vote 1 post-state OK: impls installed, migrator switched.")

    # --- TEST-ONLY: market votes must execute in deploy order ------------
    print("\n=== Negative test: Vote 3 (cbBTC) before Vote 2 (WBTC) ===")
    try:
        with boa.env.prank(YB_DAO):
            for action in market_votes[1]["actions"]:
                boa.env.raw_call(to_address=action.to, data=action.data)
        raise AssertionError(
            "Vote 3 executed before Vote 2 — ordering guard is broken."
        )
    except Exception as e:
        print(f"  Correctly reverted: {e}")

    for i, vote in enumerate(market_votes, start=2):
        spec = vote["spec"]
        print(
            f"\n=== Simulating Vote {i} ({spec['symbol']}, replaces market "
            f"#{spec['replaces_market_id']}) ==="
        )
        n_before = yb_factory.market_count()
        simulate_yb_vote(vote["actions"], label=f"Vote {i}")
        n_after = yb_factory.market_count()
        assert n_after == n_before + 1, (
            f"market_count did not increment ({n_before} -> {n_after})"
        )

    # --- TEST-ONLY: simulate Vote 6 (HybridVault market switch) ----------
    print("\n=== Simulating Vote 6 (HybridVault market switch) ===")
    simulate_yb_vote(vote6["actions"], label="Vote 6")
    for market_id in new_market_ids:
        assert hybrid_vault_factory.pool_limits(market_id) == HYBRID_POOL_LIMIT, (
            f"HybridVault pool_limits[{market_id}] not set to the new limit"
        )
    assert hybrid_vault_factory.pool_limits(CURRENT_HYBRID_POOL_ID) == 0, (
        f"old HybridVault market #{CURRENT_HYBRID_POOL_ID} still enabled"
    )
    print(
        f"  Vote 6 post-state OK: markets {new_market_ids} enabled, "
        f"old #{CURRENT_HYBRID_POOL_ID} disabled."
    )

    # --- TEST-ONLY: exercise the --activate path on the fork -------------
    # Production funds the activation account by real transfer; on the fork
    # we mint the same SEED_AMOUNTS so run_activation sees a funded account.
    for spec in POOL_SPECS:
        asset = _load_erc20(spec["coin1"])
        raw = int(round(SEED_AMOUNTS[spec["symbol"]] * 10**asset.decimals()))
        _give_asset(asset, TEST_EXECUTOR, raw)
    assert run_activation(yb_factory, pool_interface, lt_interface), \
        "run_activation reported underfunded despite minted seed"

    # --- TEST-ONLY: LTMigrator end-to-end on a real holder --------------
    new_markets = discover_new_markets(yb_factory, pool_interface)
    global GAUGE_HOLDERS_TO_SKIP
    for spec in POOL_SPECS:
        new_id = new_markets[spec["symbol"]]
        old_id = spec["replaces_market_id"]
        old_lt = lt_interface.at(yb_factory.markets(old_id).lt)

        GAUGE_HOLDERS_TO_SKIP = yb_factory.markets(old_id).staker
        holder = _find_lt_holder(old_lt.address)
        balance = old_lt.balanceOf(holder)

        # No pre-funding: the new market holds only the SEED_DEBT_CAP seed.
        # LTMigrator._migrate_plain raises lt_to's allocation itself to absorb
        # the migrated position, so the tiny debt_cap is sufficient — this is
        # exactly what the test below exercises.
        test_lt_migration(
            yb_factory, lt_interface, spec, new_id, holder, balance, migrator,
        )


if __name__ == "__main__":
    main()
