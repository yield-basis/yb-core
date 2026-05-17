#!/usr/bin/env python3
"""
Deploy new YB pools and a migrator.

Usage:
  python deploy_yb_pools_v3.py            # production deploy against NETWORK
  python deploy_yb_pools_v3.py --test     # forked dry-run via boa.fork

In test mode the script:
  1. Executes Curve Ownership-DAO vote 1408 on the fork (activates new
     Twocrypto pool implementations).
       https://etherscan.io/tx/0xf0ab2beaedc45ed0daae42a544031690d86d110e3e8dd0891bf3a25ce33b97be
  2. As TEST_EXECUTOR, deploys a WBTC/crvUSD Twocrypto pool using the
     newly-activated implementation with magic gamma (11111111111).
  3. Builds a YB Aragon-OSx vote whose action calls
     YB Factory.add_market on the new Curve pool, then simulates that
     action via raw_call from the YB DAO (per feedback_simulate_vote).
  4. From TEST_EXECUTOR, calls pool.initialize with
     [executor, LT, VirtualPool] as the initial LP allowlist.

This will eventually replace YB pool ID=3; only WBTC is wired up for now.
"""
import json
import os
import sys
import warnings

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

from networks import NETWORK


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
# LTMigrator deployed and registered as a limit setter by the
# create_vote_hybrid_factory vote. Reused — do not redeploy.
LT_MIGRATOR = "0x2cdb9f485e718f551cfeea6c33cb7062ed37066c"

# Any address; executeVote is permissionless. Also acts as the Twocrypto
# pool deployer (tx.origin), which gives it initialize() rights via the
# magic-gamma bootstrap path.
TEST_EXECUTOR = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"


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
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"  # 18 decimals

MAGIC_GAMMA = 11111111111  # Twocrypto bootstrap-mode gamma

# Per-pool spec for the new YB v3 pools. Currently only WBTC.
# `initial_price` is filled in at runtime from CoinGecko (price of coin1 in
# coin0, 1e18 precision). `coingecko_id` is the API id for coin1 in USD —
# crvUSD is treated as 1:1 USD.
POOL_SPECS = [
    {
        "name": "Yield Basis WBTC",
        "symbol": "YB-WBTC",
        "coin0": CRVUSD,  # stablecoin first, matching YB factory convention
        "coin1": WBTC,
        "coingecko_id": "bitcoin",
        "A": 5 * 10000 * 2**2,
        "mid_fee": int(0.0025 * 10**10),
        "out_fee": int(0.0045 * 10**10),
        "fee_gamma": int(0.01 * 10**18),
        "adjustment_step_min": int(0.0001 / 100 * 10**18),
        "adjustment_step_max": int(10 / 100 * 10**18),
        "ma_exp_time": 600,
        # reserved_profit_fraction in 1e10 precision (FEE_PRECISION). 50% = 5e9.
        "reserved_profit_fraction": 5 * 10**9,
        # YB add_market params (mirrors create_vote_first_markets.py)
        "leverage_fee": int(0.0092 * 10**18),
        "rate": int(0.035 * 10**18 // (86400 * 365)),
        "debt_cap": 2 * 10**6 * 10**18,
        # YB market index this pool will eventually replace
        "replaces_market_id": 3,
    },
]


COINGECKO_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"


def fetch_initial_price(coingecko_id: str) -> int:
    """Fetch coin1 USD spot from CoinGecko and return it scaled to 1e18
    (price of coin1 in coin0 = crvUSD, treated as 1 USD)."""
    response = requests.get(
        COINGECKO_PRICE_URL,
        params={"ids": coingecko_id, "vs_currencies": "usd"},
        timeout=15,
    )
    response.raise_for_status()
    price_usd = float(response.json()[coingecko_id]["usd"])
    print(f"CoinGecko {coingecko_id} = ${price_usd:,.2f}")
    return int(price_usd * 10**18)


def deploy_curve_pool(twocrypto_factory, spec: dict) -> str:
    """Deploy a bootstrap-mode Twocrypto pool from TEST_EXECUTOR using the
    implementation activated by vote 1408. Returns the new pool address."""
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


# --- YB vote: build + simulate -----------------------------------------------

def build_yb_add_market_action(factory_owner, cryptopool: str, spec: dict) -> dict:
    """Return {to, data} for a YB add_market call on `cryptopool`.
    Calls FactoryOwner.add_market (not Factory.add_market directly) — the
    factory's admin is currently a FactoryOwner contract owned by the DAO."""
    calldata = factory_owner.add_market.prepare_calldata(
        cryptopool,
        spec["leverage_fee"],
        spec["rate"],
        spec["debt_cap"],
    )
    return {"to": factory_owner.address, "data": calldata}


def build_disable_old_lt_action(factory_owner, old_lt_addr: str) -> dict:
    """Return {to, data} for a FactoryOwner call that marks the old LT as
    disabled. Required so that LTMigrator (a non-admin limit setter) can
    later call lt_allocate_stablecoins(old_lt, 0) to free its allocation."""
    calldata = factory_owner.lt_allocate_stablecoins.prepare_calldata(
        old_lt_addr, 0
    )
    return {"to": factory_owner.address, "data": calldata}


def simulate_yb_vote(actions: list[dict]):
    """Simulate a YB Aragon-OSx vote by raw_calling each action from the DAO.
    Matches what the voting plugin does on executeVote.
    See feedback_simulate_vote in repo memory."""
    print(f"Simulating YB vote with {len(actions)} action(s) from {YB_DAO}…")
    with boa.env.prank(YB_DAO):
        for action in actions:
            boa.env.raw_call(to_address=action["to"], data=action["data"])
    print("YB vote actions executed.")


# --- bootstrap initial liquidity --------------------------------------------

def seed_new_lt(new_lt, seed_assets: int):
    """Bootstrap the new LT with a small first deposit from TEST_EXECUTOR.
    The migrator's preview math divides by new_lt.totalSupply, so the LT
    must have non-zero supply before we can migrate into it."""
    erc20_partial = boa.load_partial("contracts/testing/ERC20Mock.vy")
    asset = erc20_partial.at(new_lt.ASSET_TOKEN())
    twocrypto = boa.load_partial(
        "contracts/twocrypto_pool/contracts/main/Twocrypto.vy"
    ).at(new_lt.CRYPTOPOOL())
    debt = seed_assets * twocrypto.price_oracle() // (10 ** asset.decimals())

    boa.deal(asset, TEST_EXECUTOR, seed_assets)
    with boa.env.prank(TEST_EXECUTOR):
        asset.approve(new_lt.address, 2**256 - 1)
        new_lt.deposit(seed_assets, debt, 0, TEST_EXECUTOR)

    supply = new_lt.totalSupply()
    print(f"  seeded {new_lt.symbol()}: totalSupply={supply / 1e18}")
    assert supply > 0, "seed deposit produced 0 LT shares"


# --- LTMigrator test ---------------------------------------------------------

def _find_lt_holder(lt_addr: str) -> str:
    """Return a non-gauge address with a real direct LT balance — the kind
    of user the migrator is meant to handle. Skips the gauge (top holder)
    because its balance belongs to stakers, not the gauge itself."""
    r = requests.get(
        f"https://api.ethplorer.io/getTopTokenHolders/{lt_addr}",
        params={"apiKey": "freekey", "limit": 10},
        timeout=20,
    )
    r.raise_for_status()
    for h in r.json().get("holders", []):
        addr = h["address"]
        bal = int(h["balance"])
        if addr.lower() == GAUGE_HOLDERS_TO_SKIP.lower():
            continue
        if bal > 0:
            return addr
    raise RuntimeError(f"No direct LT holder found for {lt_addr}")


def test_lt_migration(factory, lt_interface, spec: dict, new_market_id: int):
    """Use the on-chain LTMigrator to migrate a real on-chain holder's
    position from old market `spec['replaces_market_id']` to `new_market_id`.
    Raises if it fails."""
    old_market_id = spec["replaces_market_id"]
    old_lt = lt_interface.at(factory.markets(old_market_id).lt)
    new_lt = lt_interface.at(factory.markets(new_market_id).lt)
    migrator = boa.load_partial("contracts/LTMigrator.vy").at(LT_MIGRATOR)
    print(
        f"\n=== Testing LTMigrator @ {LT_MIGRATOR}: "
        f"market #{old_market_id} ({old_lt.symbol()}) -> "
        f"#{new_market_id} ({new_lt.symbol()}) ==="
    )

    global GAUGE_HOLDERS_TO_SKIP
    GAUGE_HOLDERS_TO_SKIP = factory.markets(old_market_id).staker
    holder = _find_lt_holder(old_lt.address)
    balance = old_lt.balanceOf(holder)
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


# --- pool initialization (whitelist) -----------------------------------------

def initialize_pool_whitelist(pool, executor: str, allowlist: list[str], spec: dict):
    """Call pool.initialize() from TEST_EXECUTOR with the LP allowlist.
    Requires the pool to have been deployed in bootstrap mode (magic gamma)."""
    admin_fee = 0                     # LPs get the full admin share
    with boa.env.prank(executor):
        pool.initialize(
            spec["reserved_profit_fraction"],
            admin_fee,
            "0x0000000000000000000000000000000000000000",  # policy
            spec["initial_price"],
            allowlist,
        )
    print(f"Pool initialized with allowlist: {allowlist}")


# --- main flow ---------------------------------------------------------------

def run_test_flow():
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
    assert factory_owner.ADMIN() == YB_DAO, (
        f"FactoryOwner admin is {factory_owner.ADMIN()}, expected YB DAO {YB_DAO}"
    )

    ensure_curve_vote_executed(CURVE_VOTE_ID)
    activated = twocrypto_factory.pool_implementations(NEW_TWOCRYPTO_IMPL_ID)
    print(f"Activated pool implementation: {activated}")
    assert activated != "0x0000000000000000000000000000000000000000", (
        "Implementation slot is empty after vote 1408 — wrong id?"
    )

    boa.env.set_balance(TEST_EXECUTOR, 100 * 10**18)

    for spec in POOL_SPECS:
        print(
            f"\n=== Deploying YB v3 pool replacing market "
            f"#{spec['replaces_market_id']} ==="
        )

        spec["initial_price"] = fetch_initial_price(spec["coingecko_id"])
        pool_addr = deploy_curve_pool(twocrypto_factory, spec)
        pool = pool_interface.at(pool_addr)

        n_before = yb_factory.market_count()
        old_lt_addr = yb_factory.markets(spec["replaces_market_id"]).lt
        actions = [
            build_yb_add_market_action(factory_owner, pool_addr, spec),
            build_disable_old_lt_action(factory_owner, old_lt_addr),
        ]
        simulate_yb_vote(actions)
        n_after = yb_factory.market_count()
        assert n_after == n_before + 1, (
            f"market_count did not increment ({n_before} -> {n_after})"
        )

        new_market = yb_factory.markets(n_after - 1)
        lt_addr = new_market.lt
        vpool_addr = new_market.virtual_pool
        print(f"New YB market #{n_after - 1}: lt={lt_addr} virtual_pool={vpool_addr}")

        initialize_pool_whitelist(
            pool, TEST_EXECUTOR, [TEST_EXECUTOR, lt_addr, vpool_addr], spec
        )

        # 5. Seed the new LT with a small first deposit so the migrator's
        #    proportional math has non-zero supply to divide against.
        new_lt = lt_interface.at(lt_addr)
        seed_new_lt(new_lt, 10**5)  # 0.001 WBTC (8 decimals)

        # 6. Verify the existing on-chain LTMigrator can move funds from
        #    the old market into the new one.
        test_lt_migration(yb_factory, lt_interface, spec, n_after - 1)


# --- entrypoint --------------------------------------------------------------

def main():
    test_mode = "--test" in sys.argv[1:]

    if test_mode:
        boa.fork(NETWORK)
        boa.env.eoa = TEST_EXECUTOR
        run_test_flow()
    else:
        from eth_account import account
        from getpass import getpass

        key_path = os.path.expanduser(
            os.path.join("~", ".brownie", "accounts", "yb-deployer.json")
        )
        with open(key_path, "r") as f:
            pkey = account.decode_keyfile_json(json.load(f), getpass())
        signer = account.Account.from_key(pkey)

        boa.set_network_env(NETWORK)
        boa.env.add_account(signer)

        raise NotImplementedError(
            "Production deploy path (real createProposal + on-chain pool "
            "deploy) not wired up yet."
        )


if __name__ == "__main__":
    main()
