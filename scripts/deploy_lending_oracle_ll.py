#!/usr/bin/env python3
"""
Deploy the YBLendingOracleLL implementation + YBLendingOracleLLFactory, then spawn the
per-market EMA-smoothed price oracles (USD + asset) for each market in MARKET_IDS.

The implementation is deployed first (no constructor args) and passed into the factory, which
clones it per (market, denomination) via create_oracles(market_id). Each clone is an
EIP-1167 proxy holding its own (LT, in_usd, ema_time) + virtual_price EMA state. The EMA is
unseeded until the first price_w(); the YB Factory admin (DAO) can retune any clone's ema_time
via the factory.

    FORK = True   -> deploy on a fork, create the oracles and sanity-check price() / price_w().
    FORK = False  -> broadcast on mainnet, verify impl + factory on Etherscan, create oracles.

    python scripts/deploy_lending_oracle_ll.py
"""
import boa
import os
import json
import warnings
from time import sleep
from eth_account import account
from getpass import getpass
from eth_utils import keccak
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK
from networks import ETHERSCAN_API_KEY

# Reading a clone via the impl ABI makes boa compare clone vs impl bytecode; harmless.
warnings.filterwarnings("ignore", message="casted bytecode does not match compiled bytecode",
                        category=UserWarning)

FORK = False
EXTRA_TIMEOUT = 10
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"   # YB Factory (market-id -> LT lookup)
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"       # Aragon DAO: may retune ema_time
MARKET_IDS = [7, 8, 9, 10]                               # markets to create EMA oracles for
EMA_TIME = 866                                           # default EMA time (s): ~10 min half-life
SEED_RETRIES = 40                                        # price_w() retries through node desyncs
SEED_WAIT = 15                                           # seconds between retries (node ~12s/block)
# Set to an already-deployed YBLendingOracleLLFactory to RESUME against it (skip deploying a new
# impl+factory): re-runs create_oracles (idempotent) + seeding + ENS. Use to finish a run that
# died mid-way (e.g. a flaky-RPC crash) without deploying another throwaway factory. "" = deploy.
EXISTING_FACTORY = "0x3e6B4795bd173Dd5c700cA8Cfd3f247BFcDC9D43"

# --- ENS (optional) ----------------------------------------------------------
# When SET_ENS, after deploying + seeding the oracles, (re)write the oracles-ll.yieldbasis.eth
# subtree: oracles-ll -> factory, <label>.oracles-ll -> asset oracle, usd.<label>.oracles-ll ->
# usd oracle. All names are wrapped, normally owned by ENS_OWNER (a YB multisig).
#
# ENS record auth is per-node and doesn't inherit down the tree, so writing every record needs
# authority on every node. Rather than N approvals, this uses ONE scoped multisig action: the
# owner transfers the wrapped `oracles-ll` name to the deployer. The deployer then owns the
# parent, so it can re-own each child (setSubnodeRecord both creates a missing child AND takes
# over an existing one), setAddr it, and hand each child back to ENS_OWNER - then transfer the
# parent itself back. So the multisig does exactly one thing (transfer in); the deployer returns
# everything. Trade-off vs an approval: the deployer holds full ownership of `oracles-ll` for the
# duration of the run (scoped to that name only), so it relies on the deployer key for that window.
# In FORK mode the transfer in/out is simulated; for a real broadcast the multisig does the one
# transfer-in first (the exact call is printed) and the script returns ownership at the end.
SET_ENS = True
ENS_PARENT = "oracles-ll.yieldbasis.eth"
ENS_LABELS = {7: "wbtc", 8: "cbbtc", 9: "tbtc", 10: "weth"}
ENS_OWNER = "0xC1671c9efc9A2ecC347238BeA054Fc6d1c6c28F9"   # wrapped-name owner; subnames end up here
NAME_WRAPPER = "0xD4416b13d2b3a9aBae7AcD5D6C2BbDBE25686401"
PUBLIC_RESOLVER = "0x231b0Ee14048e9dCcD1d247744d114a4EB5E8E63"

NAME_WRAPPER_ABI = json.dumps([
    {"name": "setSubnodeRecord", "stateMutability": "nonpayable", "type": "function", "inputs": [
        {"type": "bytes32"}, {"type": "string"}, {"type": "address"}, {"type": "address"},
        {"type": "uint64"}, {"type": "uint32"}, {"type": "uint64"}], "outputs": [{"type": "bytes32"}]},
    {"name": "safeTransferFrom", "stateMutability": "nonpayable", "type": "function", "inputs": [
        {"type": "address"}, {"type": "address"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "bytes"}], "outputs": []},
    {"name": "ownerOf", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]},
    {"name": "getData", "stateMutability": "view", "type": "function", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "address"}, {"type": "uint32"}, {"type": "uint64"}]},
])
RESOLVER_ABI = json.dumps([
    {"name": "setAddr", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "bytes32"}, {"type": "address"}], "outputs": []},
    {"name": "addr", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "bytes32"}], "outputs": [{"type": "address"}]},
])


def namehash(name):
    node = b"\x00" * 32
    for label in reversed(name.split(".")):
        node = keccak(node + keccak(text=label))
    return node


def _tokid(node):
    return int.from_bytes(node, "big")


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


def verify(*args, **kw):
    while True:
        try:
            sleep(EXTRA_TIMEOUT)
            boa_verify(*args, **kw)
            break
        except ValueError as e:
            print(e)
            if "Already Verified" in str(e):
                return


def _seed(o):
    """price_w() a clone to (re)checkpoint its fundamental EMA. ALWAYS runs - even if already
    seeded, since re-checkpointing is harmless and doubles as a live prod sanity check each run.
    price_w() MUST succeed, so a transient node/RPC desync (e.g. a stale-fork OOG) is retried,
    waiting for the node to settle between attempts - never skipped. Returns the price."""
    last = None
    for attempt in range(SEED_RETRIES):
        try:
            return o.price_w()
        except Exception as e:
            last = e
            print(f"    price_w({o.address}) attempt {attempt + 1}/{SEED_RETRIES} failed "
                  f"({str(e)[:70]}); waiting {SEED_WAIT}s for the node to sync...")
            sleep(SEED_WAIT)
    raise Exception(f"price_w({o.address}) still failing after {SEED_RETRIES} retries: {last}")


def validate_existing():
    """Preflight for EXISTING_FACTORY: on a throwaway fork, assert the deployed impl + factory +
    clones are byte-identical to the current source and correctly parameterized. Returns a list
    of problem strings (empty == safe to resume). Runs before the real env is set up, so it opens
    its own fork with no nesting.

    Bytecode: the impl has no immutables so it compares directly to a fresh reference; the factory
    embeds (FACTORY, LL_IMPL) as immutables so we deploy a reference with the SAME LL_IMPL and
    compare; each clone must be exactly the EIP-1167 proxy for LL_IMPL.
    """
    problems = []
    zero = "0x0000000000000000000000000000000000000000"
    with boa.fork(NETWORK, block_identifier="latest"):
        exf = boa.load_partial('contracts/utils/YBLendingOracleLLFactory.vy').at(EXISTING_FACTORY)
        exist_impl = exf.LL_IMPL()
        yb_factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
        ll = boa.load_partial('contracts/utils/YBLendingOracleLL.vy')

        # --- bytecode ---
        ref_impl = boa.load('contracts/utils/YBLendingOracleLL.vy')
        if boa.env.get_code(exist_impl) != boa.env.get_code(ref_impl.address):
            problems.append(f"impl {exist_impl} bytecode != source")
        ref_factory = boa.load('contracts/utils/YBLendingOracleLLFactory.vy',
                               FACTORY, exist_impl, EMA_TIME, DAO)
        if boa.env.get_code(EXISTING_FACTORY) != boa.env.get_code(ref_factory.address):
            problems.append(f"factory {EXISTING_FACTORY} bytecode != source")
        proxy = (bytes.fromhex("363d3d373d3d3d363d73") + bytes.fromhex(exist_impl[2:])
                 + bytes.fromhex("5af43d82803e903d91602b57fd5bf3"))

        # --- factory params ---
        if exf.FACTORY() != FACTORY:
            problems.append(f"factory.FACTORY {exf.FACTORY()} != {FACTORY}")
        if exf.dao() != DAO:
            problems.append(f"factory.dao {exf.dao()} != {DAO}")
        if exf.default_ema_time() != EMA_TIME:
            problems.append(f"factory.default_ema_time {exf.default_ema_time()} != {EMA_TIME}")

        # --- clone bytecode + params (skip markets not created yet; the run creates those) ---
        for mid in MARKET_IDS:
            lt = yb_factory.markets(mid).lt
            for addr, in_usd, tag in ((exf.asset_oracle(mid), False, "asset"),
                                      (exf.usd_oracle(mid), True, "usd")):
                if addr == zero:
                    continue
                if boa.env.get_code(addr) != proxy:
                    problems.append(f"market {mid} {tag} clone {addr} is not the impl EIP-1167 proxy")
                o = ll.at(addr)
                if o.lt_token() != lt:
                    problems.append(f"market {mid} {tag} lt_token {o.lt_token()} != {lt}")
                if o.in_usd() != in_usd:
                    problems.append(f"market {mid} {tag} in_usd {o.in_usd()} != {in_usd}")
                if o.factory() != EXISTING_FACTORY:
                    problems.append(f"market {mid} {tag} factory {o.factory()} != {EXISTING_FACTORY}")
                if o.ema_time() != EMA_TIME:
                    problems.append(f"market {mid} {tag} ema_time {o.ema_time()} != {EMA_TIME}")
    return problems


def set_ens(created, factory_addr):
    """(Re)write the oracles-ll.yieldbasis.eth subtree to the freshly deployed factory + clones.
    Requires the active account to already own the wrapped `oracles-ll` name (the one multisig
    transfer). Per child: take ownership (creates it if missing), setAddr, then hand it back to
    ENS_OWNER; finally transfer the parent back to ENS_OWNER."""
    nw = boa.loads_abi(NAME_WRAPPER_ABI).at(NAME_WRAPPER)
    res = boa.loads_abi(RESOLVER_ABI).at(PUBLIC_RESOLVER)
    parent = namehash(ENS_PARENT)
    me = str(boa.env.eoa)
    exp = nw.getData(_tokid(parent))[2]                     # mirror the parent's expiry on children
    assert nw.ownerOf(_tokid(parent)) == me, "deployer must own oracles-ll (multisig transfer first)"

    print(f"\n--- ENS: rewriting {ENS_PARENT} subtree ---")
    res.setAddr(parent, factory_addr)
    print(f"    ens {ENS_PARENT} -> {factory_addr} (factory)")
    for mid, lt, usd, asset, usd_price, asset_price, pps in created:
        label = ENS_LABELS[mid]
        anode = keccak(parent + keccak(text=label))
        unode = keccak(anode + keccak(text="usd"))
        nw.setSubnodeRecord(parent, label, me, PUBLIC_RESOLVER, 0, 0, exp)    # own <label>.oracles-ll
        res.setAddr(anode, asset)
        nw.setSubnodeRecord(anode, "usd", me, PUBLIC_RESOLVER, 0, 0, exp)     # own usd.<label>.oracles-ll
        res.setAddr(unode, usd)
        nw.setSubnodeRecord(anode, "usd", ENS_OWNER, PUBLIC_RESOLVER, 0, 0, exp)   # usd -> owner
        nw.setSubnodeRecord(parent, label, ENS_OWNER, PUBLIC_RESOLVER, 0, 0, exp)  # <label> -> owner
        print(f"    ens {label}.<parent> -> {asset}   usd.{label}.<parent> -> {usd}")
    nw.safeTransferFrom(me, ENS_OWNER, _tokid(parent), 1, b"")               # return the parent


if __name__ == '__main__':
    if EXISTING_FACTORY:
        # Only trust an existing deployment if it's byte-for-byte the current source and correctly
        # wired; otherwise abort so the operator can redeploy fresh (set EXISTING_FACTORY = "").
        _problems = validate_existing()
        if _problems:
            print(f"\n!!! EXISTING_FACTORY {EXISTING_FACTORY} does NOT match source:")
            for _p in _problems:
                print(f"  - {_p}")
            raise SystemExit("existing deployment differs; set EXISTING_FACTORY = \"\" to redeploy fresh")
        print(f"EXISTING_FACTORY {EXISTING_FACTORY}: bytecode + params match source - safe to resume")

    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = DEPLOYER
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)

    ll_factory_d = boa.load_partial('contracts/utils/YBLendingOracleLLFactory.vy')
    if EXISTING_FACTORY:
        # Resume: attach to the already-deployed factory; skip deploying a new impl + factory.
        ll_factory = ll_factory_d.at(EXISTING_FACTORY)
        print(f"YBLendingOracleLLFactory (existing): {ll_factory.address}  impl {ll_factory.LL_IMPL()}")
    else:
        impl = boa.load('contracts/utils/YBLendingOracleLL.vy')
        if not FORK:
            verify(impl, etherscan, wait=True)
        print(f"YBLendingOracleLL impl: {impl.address}")

        ll_factory = boa.load('contracts/utils/YBLendingOracleLLFactory.vy', FACTORY, impl.address, EMA_TIME, DAO)
        if not FORK:
            verify(ll_factory, etherscan, wait=True)
        print(f"YBLendingOracleLLFactory: {ll_factory.address}")

    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    ll = boa.load_partial('contracts/utils/YBLendingOracleLL.vy')
    lt_d = boa.load_partial('contracts/LT.vy')

    created = []
    for mid in MARKET_IDS:
        lt = factory.markets(mid).lt
        assert lt != "0x0000000000000000000000000000000000000000", f"market {mid} has no LT"
        # Read existing clones (view, no tx); only create_oracles when a market isn't set up yet -
        # so a resume against an already-populated factory doesn't broadcast redundant creates.
        usd, asset = ll_factory.usd_oracle(mid), ll_factory.asset_oracle(mid)
        if usd == "0x0000000000000000000000000000000000000000":
            usd, asset = ll_factory.create_oracles(mid)
        usd_o = ll.at(usd)
        asset_o = ll.at(asset)
        # Checkpoint each clone's fundamental EMA via price_w() - always, on every run (harmless
        # if already seeded; a live prod check). price_w() must succeed, so _seed retries through
        # transient node desyncs rather than skipping.
        usd_price = _seed(usd_o)
        asset_price = _seed(asset_o)
        pps = lt_d.at(lt).pricePerShare()          # redemption coefficient (fair value/share)
        if FORK:
            # Clones must be wired correctly, seeded, and creation is idempotent.
            assert usd_o.lt_token() == lt and asset_o.lt_token() == lt, f"market {mid} wrong LT"
            assert usd_o.in_usd() and not asset_o.in_usd(), f"market {mid} denom"
            assert usd_o.factory() == ll_factory.address, f"market {mid} factory"
            assert usd_o.ema_time() == EMA_TIME and asset_o.ema_time() == EMA_TIME, f"market {mid} ema"
            assert usd_price > 0 and asset_price > 0, f"market {mid} zero price"
            assert usd_o.fundamental_ema() > 0 and asset_o.fundamental_ema() > 0, f"market {mid} unseeded"
            assert ll_factory.create_oracles(mid) == (usd, asset), f"market {mid} not idempotent"
        created.append((mid, lt, usd, asset, usd_price, asset_price, pps))
        print(f"market {mid}: created + seeded EMA oracles")

    print("\n==================== deployment ====================")
    print(f"YBLendingOracleLL impl   : {ll_factory.LL_IMPL()}")
    print(f"YBLendingOracleLLFactory : {ll_factory.address}")
    print(f"  FACTORY                : {ll_factory.FACTORY()}")
    print(f"  LL_IMPL                : {ll_factory.LL_IMPL()}")
    print(f"  dao                    : {ll_factory.dao()}")
    print(f"  default_ema_time       : {ll_factory.default_ema_time()} s")
    print("---------------- per-market EMA oracles ----------------")
    for mid, lt, usd, asset, usd_price, asset_price, pps in created:
        print(f"market {mid}  LT {lt}  ({ENS_LABELS.get(mid, '?')})")
        print(f"    usd_oracle    : {usd}   price() = {usd_price/1e18:.2f}")
        print(f"    asset_oracle  : {asset}   price() = {asset_price/1e18:.4f}")
        print(f"    pricePerShare : {pps/1e18:.6f}")
    print("====================================================")

    if SET_ENS:
        nw = boa.loads_abi(NAME_WRAPPER_ABI).at(NAME_WRAPPER)
        eoa = str(boa.env.eoa)
        pid = _tokid(namehash(ENS_PARENT))
        transfer_in = (f"NameWrapper.safeTransferFrom({ENS_OWNER}, {DEPLOYER}, {pid}, 1, 0x)"
                       f"  (one call, from {ENS_OWNER})")
        if FORK:
            # Ensure the deployer owns oracles-ll: simulate the one multisig transfer-in from the
            # current owner (a no-op if the deployer already holds it). The script returns it.
            cur = nw.ownerOf(pid)
            if cur != eoa:
                with boa.env.prank(cur):
                    nw.safeTransferFrom(cur, eoa, pid, 1, b"")
            set_ens(created, ll_factory.address)
            pr = boa.loads_abi(RESOLVER_ABI).at(PUBLIC_RESOLVER)
            assert pr.addr(namehash(ENS_PARENT)) == ll_factory.address, "parent addr not set"
            assert nw.ownerOf(pid) == ENS_OWNER, "parent not returned to owner"
            print("ENS subtree rewritten + oracles-ll returned (fork-simulated transfer) - OK")
        else:
            # Real broadcast: the multisig must have transferred oracles-ll to the deployer first.
            assert nw.ownerOf(pid) == eoa, \
                f"deployer must own {ENS_PARENT} first (one multisig action):\n  {transfer_in}"
            set_ens(created, ll_factory.address)
            print(f"\nENS subtree written; {ENS_PARENT} returned to {ENS_OWNER}.")
