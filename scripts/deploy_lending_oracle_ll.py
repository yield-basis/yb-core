#!/usr/bin/env python3
"""
Deploy the YBLendingOracleLL implementation + YBLendingOracleLLUSD blueprint +
YBLendingOracleLLFactory, then spawn the per-market EMA-smoothed price oracles (USD + asset) for
each market in MARKET_IDS.

The implementation (no constructor args) and the blueprint are deployed first and passed into the
factory. Per market, create_oracles(market_id) clones the implementation as the asset oracle - an
EIP-1167 proxy holding (LT, ema_time) and the market's ONE fundamental EMA - and deploys a
YBLendingOracleLLUSD forwarder onto it from the blueprint, so USD and asset checkpoints advance
the same EMA. The EMA is unseeded until the first price_w() (via either side); the YB Factory
admin (DAO) can retune a market's ema_time via the factory.

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
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK
from networks import ETHERSCAN_API_KEY
import oracle_ens
from oracle_ens import ENS_LABELS

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
# 0x3e6B4795bd173Dd5c700cA8Cfd3f247BFcDC9D43 is the pre-#002 factory (separate USD/asset EMAs),
# superseded by the shared-EMA version; 0x150dD481D306A3a8577cfc8fAC195720AA3Ee7Bc is the
# shared-EMA factory, superseded by 0x1C4E1a6E8e470753eAA63888A6b588c8fa06e600 (current: untradable
# balance value + safe-debt limits, see docs/oracles_addresses.md).
EXISTING_FACTORY = ""

# --- ENS (optional) ----------------------------------------------------------
# When SET_ENS, after deploying + seeding the oracles, (re)write the oracles-ll.yieldbasis.eth
# subtree: oracles-ll -> factory, <label>.oracles-ll -> asset oracle, usd.<label>.oracles-ll ->
# usd oracle (see oracle_ens.py). A broadcast run needs ONE multisig action first: ENS_OWNER
# transfers the wrapped `oracles-ll` name to the deployer (the script checks this before
# deploying and prints the exact call); the script hands every name back at the end.
SET_ENS = True
ENS_PARENT = "oracles-ll.yieldbasis.eth"


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.Account.decrypt(json.load(f), getpass())
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

    Bytecode: the impl and the USD blueprint have no immutables so they compare directly to fresh
    references; the factory embeds (FACTORY, LL_IMPL, USD_BLUEPRINT) as immutables so we deploy a
    reference with the SAME addresses and compare; each asset clone must be exactly the EIP-1167
    proxy for LL_IMPL, and each USD forwarder must match a reference forwarder onto that clone.
    """
    problems = []
    zero = "0x0000000000000000000000000000000000000000"
    with boa.fork(NETWORK, block_identifier="latest"):
        exf = boa.load_partial('contracts/utils/YBLendingOracleLLFactory.vy').at(EXISTING_FACTORY)
        exist_impl = exf.LL_IMPL()
        exist_bp = exf.USD_BLUEPRINT()
        yb_factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
        ll = boa.load_partial('contracts/utils/YBLendingOracleLL.vy')
        ll_usd_d = boa.load_partial('contracts/utils/YBLendingOracleLLUSD.vy')

        # --- bytecode ---
        ref_impl = boa.load('contracts/utils/YBLendingOracleLL.vy')
        if boa.env.get_code(exist_impl) != boa.env.get_code(ref_impl.address):
            problems.append(f"impl {exist_impl} bytecode != source")
        ref_bp = ll_usd_d.deploy_as_blueprint()
        if boa.env.get_code(exist_bp) != boa.env.get_code(ref_bp.address):
            problems.append(f"usd blueprint {exist_bp} bytecode != source")
        ref_factory = boa.load('contracts/utils/YBLendingOracleLLFactory.vy',
                               FACTORY, exist_impl, exist_bp, EMA_TIME, DAO)
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

        # --- oracle bytecode + params (skip markets not created yet; the run creates those) ---
        for mid in MARKET_IDS:
            lt = yb_factory.markets(mid).lt
            asset, usd = exf.asset_oracle(mid), exf.usd_oracle(mid)
            if asset == zero:
                continue
            if boa.env.get_code(asset) != proxy:
                problems.append(f"market {mid} asset clone {asset} is not the impl EIP-1167 proxy")
            o = ll.at(asset)
            if o.lt_token() != lt:
                problems.append(f"market {mid} lt_token {o.lt_token()} != {lt}")
            if o.factory() != EXISTING_FACTORY:
                problems.append(f"market {mid} factory {o.factory()} != {EXISTING_FACTORY}")
            if o.ema_time() != EMA_TIME:
                problems.append(f"market {mid} ema_time {o.ema_time()} != {EMA_TIME}")
            ref_usd = ll_usd_d.deploy(asset)
            if boa.env.get_code(usd) != boa.env.get_code(ref_usd.address):
                problems.append(f"market {mid} usd forwarder {usd} bytecode != source onto {asset}")
    return problems


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
        # A tx priced near the base fee can take > boa's default 240s to land; don't abort mid-run.
        boa.env.tx_settings.poll_timeout = 900
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)
        if SET_ENS:
            oracle_ens.require_owner(ENS_PARENT)

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

        usd_bp = boa.load_partial('contracts/utils/YBLendingOracleLLUSD.vy').deploy_as_blueprint()
        if not FORK:
            verify(usd_bp, etherscan, wait=True)
        print(f"YBLendingOracleLLUSD blueprint: {usd_bp.address}")

        ll_factory = boa.load('contracts/utils/YBLendingOracleLLFactory.vy', FACTORY, impl.address,
                              usd_bp.address, EMA_TIME, DAO)
        if not FORK:
            verify(ll_factory, etherscan, wait=True)
        print(f"YBLendingOracleLLFactory: {ll_factory.address}")

    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    ll = boa.load_partial('contracts/utils/YBLendingOracleLL.vy')
    ll_usd = boa.load_partial('contracts/utils/YBLendingOracleLLUSD.vy')
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
        usd_o = ll_usd.at(usd)
        asset_o = ll.at(asset)
        # Checkpoint the market's (shared) fundamental EMA via price_w() on both sides - always,
        # on every run (harmless if already seeded; a live prod check of both paths). price_w()
        # must succeed, so _seed retries through transient node desyncs rather than skipping.
        usd_price = _seed(usd_o)
        asset_price = _seed(asset_o)
        pps = lt_d.at(lt).pricePerShare()          # redemption coefficient (fair value/share)
        if FORK:
            # Oracles must be wired correctly, seeded, and creation is idempotent.
            assert asset_o.lt_token() == lt, f"market {mid} wrong LT"
            assert usd_o.ORACLE() == asset, f"market {mid} usd forwarder not onto the asset oracle"
            assert asset_o.factory() == ll_factory.address, f"market {mid} factory"
            assert asset_o.ema_time() == EMA_TIME, f"market {mid} ema"
            assert usd_price > 0 and asset_price > 0, f"market {mid} zero price"
            assert usd_o.price() == asset_o.price_usd(), f"market {mid} usd forwarder mismatch"
            assert asset_o.fundamental_ema() > 0, f"market {mid} unseeded"
            assert ll_factory.create_oracles(mid) == (usd, asset), f"market {mid} not idempotent"
        created.append((mid, lt, usd, asset, usd_price, asset_price, pps))
        print(f"market {mid}: created + seeded EMA oracles")

    print("\n==================== deployment ====================")
    print(f"YBLendingOracleLL impl   : {ll_factory.LL_IMPL()}")
    print(f"YBLendingOracleLLUSD bp  : {ll_factory.USD_BLUEPRINT()}")
    print(f"YBLendingOracleLLFactory : {ll_factory.address}")
    print(f"  FACTORY                : {ll_factory.FACTORY()}")
    print(f"  LL_IMPL                : {ll_factory.LL_IMPL()}")
    print(f"  USD_BLUEPRINT          : {ll_factory.USD_BLUEPRINT()}")
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
        oracle_ens.write_subtree(ENS_PARENT, ll_factory.address,
                                 [(mid, usd, asset) for mid, lt, usd, asset, *_ in created], FORK)
