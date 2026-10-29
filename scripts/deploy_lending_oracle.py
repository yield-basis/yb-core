#!/usr/bin/env python3
"""
Deploy the YBLendingOracle singleton together with its YBPriceProxy implementation, then
spawn the per-market price() proxies (USD + asset) for each market in MARKET_IDS and point the
oracles.yieldbasis.eth ENS subtree at them.

The proxy impl is deployed first (no constructor args) and passed into the oracle, which
clones it per market via create_oracles(market_id).

    FORK = True   -> deploy on a fork, create the proxies and sanity-check price() + ENS.
    FORK = False  -> broadcast on mainnet, verify the impl + oracle on Etherscan, create proxies.

All deployed / created addresses are printed at the end.

    python scripts/deploy_lending_oracle.py
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
MARKET_IDS = [7, 8, 9, 10]                               # markets to create price() proxies for
# Set to an already-deployed YBLendingOracle to RESUME against it (skip deploying a new impl +
# singleton): creates the missing proxies (create_oracles is idempotent) + ENS. Use to finish a
# run that died mid-way (e.g. a receipt the RPC never reported) without deploying another
# throwaway singleton. "" = deploy fresh.
# 0x58241c11abd0BDb1448EF9f38f8AA7Fda21a3A44 is the pre-safe-limits singleton, superseded by
# 0x61BDFf6850ad7bBD67D3307d5c608C6e4cb3158f (A at the last trade: lags idle A ramps), superseded by
# 0xB2dC48d8C09094e8426376B3C9090Eca8c11bD29 (current: live A, see docs/oracles_addresses.md).
EXISTING_ORACLE = ""

# --- ENS (optional) ----------------------------------------------------------
# When SET_ENS, after creating the proxies, (re)write the oracles.yieldbasis.eth subtree:
# oracles -> singleton, <label>.oracles -> asset proxy, usd.<label>.oracles -> usd proxy (see
# oracle_ens.py). A broadcast run needs ONE multisig action first: ENS_OWNER transfers the
# wrapped `oracles` name to the deployer (the script checks this before deploying and prints the
# exact call); the script hands every name back at the end.
SET_ENS = True
ENS_PARENT = "oracles.yieldbasis.eth"
ZERO = "0x0000000000000000000000000000000000000000"


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


def validate_existing():
    """Preflight for EXISTING_ORACLE: on a throwaway fork, assert the deployed impl + singleton +
    proxies are byte-identical to the current source and correctly wired. Returns a list of
    problem strings (empty == safe to resume). Runs before the real env is set up, so it opens
    its own fork with no nesting.

    The impl has no immutables so it compares directly to a fresh reference; the singleton embeds
    (FACTORY, PROXY_IMPL) as immutables so we deploy a reference with the SAME addresses and
    compare; each proxy must be exactly the EIP-1167 proxy for PROXY_IMPL, bound to this
    singleton and the market's LT.
    """
    problems = []
    with boa.fork(NETWORK, block_identifier="latest"):
        oracle_d = boa.load_partial('contracts/utils/YBLendingOracle.vy')
        exo = oracle_d.at(EXISTING_ORACLE)
        exist_impl = exo.PROXY_IMPL()
        yb_factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
        proxy_d = boa.load_partial('contracts/utils/YBPriceProxy.vy')

        # --- bytecode ---
        ref_impl = proxy_d.deploy()
        if boa.env.get_code(exist_impl) != boa.env.get_code(ref_impl.address):
            problems.append(f"proxy impl {exist_impl} bytecode != source")
        ref_oracle = oracle_d.deploy(FACTORY, exist_impl)
        if boa.env.get_code(EXISTING_ORACLE) != boa.env.get_code(ref_oracle.address):
            problems.append(f"singleton {EXISTING_ORACLE} bytecode != source")
        clone = (bytes.fromhex("363d3d373d3d3d363d73") + bytes.fromhex(exist_impl[2:])
                 + bytes.fromhex("5af43d82803e903d91602b57fd5bf3"))

        # --- params ---
        if exo.FACTORY() != FACTORY:
            problems.append(f"singleton.FACTORY {exo.FACTORY()} != {FACTORY}")

        # --- proxies (skip markets not created yet; the run creates those) ---
        for mid in MARKET_IDS:
            lt = yb_factory.markets(mid).lt
            for addr, in_usd in ((exo.usd_oracle(mid), True), (exo.asset_oracle(mid), False)):
                if addr == ZERO:
                    continue
                if boa.env.get_code(addr) != clone:
                    problems.append(f"market {mid} proxy {addr} is not the impl EIP-1167 proxy")
                p = proxy_d.at(addr)
                if (p.oracle(), p.lt(), p.in_usd()) != (EXISTING_ORACLE, lt, in_usd):
                    problems.append(f"market {mid} proxy {addr} bound to "
                                    f"{(p.oracle(), p.lt(), p.in_usd())}, expected {(EXISTING_ORACLE, lt, in_usd)}")
    return problems


if __name__ == '__main__':
    if EXISTING_ORACLE:
        # Only trust an existing deployment if it's byte-for-byte the current source and correctly
        # wired; otherwise abort so the operator can redeploy fresh (set EXISTING_ORACLE = "").
        _problems = validate_existing()
        if _problems:
            print(f"\n!!! EXISTING_ORACLE {EXISTING_ORACLE} does NOT match source:")
            for _p in _problems:
                print(f"  - {_p}")
            raise SystemExit("existing deployment differs; set EXISTING_ORACLE = \"\" to redeploy fresh")
        print(f"EXISTING_ORACLE {EXISTING_ORACLE}: bytecode + params match source - safe to resume")

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

    oracle_d = boa.load_partial('contracts/utils/YBLendingOracle.vy')
    if EXISTING_ORACLE:
        # Resume: attach to the already-deployed singleton; skip deploying a new impl + singleton.
        oracle = oracle_d.at(EXISTING_ORACLE)
        print(f"YBLendingOracle (existing): {oracle.address}  impl {oracle.PROXY_IMPL()}")
    else:
        proxy_impl = boa.load('contracts/utils/YBPriceProxy.vy')
        if not FORK:
            verify(proxy_impl, etherscan, wait=True)
        print(f"YBPriceProxy impl: {proxy_impl.address}")

        oracle = boa.load('contracts/utils/YBLendingOracle.vy', FACTORY, proxy_impl.address)
        if not FORK:
            verify(oracle, etherscan, wait=True)
        print(f"YBLendingOracle:   {oracle.address}")

    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    proxy = boa.load_partial('contracts/utils/YBPriceProxy.vy')

    created = []
    for mid in MARKET_IDS:
        lt = factory.markets(mid).lt
        assert lt != ZERO, f"market {mid} has no LT"
        # Read existing proxies (view, no tx); only create_oracles when a market isn't set up yet -
        # so a resume doesn't broadcast redundant creates.
        usd, asset = oracle.usd_oracle(mid), oracle.asset_oracle(mid)
        if usd == ZERO:
            usd, asset = oracle.create_oracles(mid)
        usd_price = proxy.at(usd).price()
        asset_price = proxy.at(asset).price()
        if FORK:
            # The proxies must forward to the singleton bit-for-bit, and creation is idempotent.
            assert usd_price == oracle.price_in_usd(lt), f"market {mid} usd proxy != singleton"
            assert asset_price == oracle.price_in_asset(lt), f"market {mid} asset proxy != singleton"
            assert oracle.create_oracles(mid) == (usd, asset), f"market {mid} not idempotent"
        created.append((mid, lt, usd, asset, usd_price, asset_price))
        print(f"market {mid}: created proxies")

    print("\n==================== deployment ====================")
    print(f"YBPriceProxy impl : {oracle.PROXY_IMPL()}")
    print(f"YBLendingOracle   : {oracle.address}")
    print(f"  FACTORY         : {oracle.FACTORY()}")
    print(f"  PROXY_IMPL      : {oracle.PROXY_IMPL()}")
    print("---------------- per-market price() proxies ----------------")
    for mid, lt, usd, asset, usd_price, asset_price in created:
        print(f"market {mid}  LT {lt}  ({ENS_LABELS.get(mid, '?')})")
        print(f"    usd_oracle   : {usd}   price() = {usd_price/1e18:.2f}")
        print(f"    asset_oracle : {asset}   price() = {asset_price/1e18:.4f}")
    print("====================================================")

    if SET_ENS:
        oracle_ens.write_subtree(ENS_PARENT, oracle.address,
                                 [(mid, usd, asset) for mid, lt, usd, asset, *_ in created], FORK)
