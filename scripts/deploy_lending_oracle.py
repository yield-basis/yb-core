#!/usr/bin/env python3
"""
Deploy the YBLendingOracle singleton together with its YBPriceProxy implementation, then
spawn the per-market price() proxies (USD + asset) for each market in MARKET_IDS.

The proxy impl is deployed first (no constructor args) and passed into the oracle, which
clones it per market via create_oracles(market_id).

    FORK = True   -> deploy on a fork, create the proxies and sanity-check price().
    FORK = False  -> broadcast on mainnet, verify the impl + oracle on Etherscan, create proxies.

All deployed / created addresses are printed at the end.

    python scripts/deploy_lending_oracle.py
"""
import boa
import os
import json
from time import sleep
from eth_account import account
from getpass import getpass
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK
from networks import ETHERSCAN_API_KEY


FORK = False
EXTRA_TIMEOUT = 10
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"   # YB Factory (market-id -> LT lookup)
MARKET_IDS = [7, 8, 9, 10]                               # markets to create price() proxies for


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


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = DEPLOYER
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)

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
        assert lt != "0x0000000000000000000000000000000000000000", f"market {mid} has no LT"
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
    print(f"YBPriceProxy impl : {proxy_impl.address}")
    print(f"YBLendingOracle   : {oracle.address}")
    print(f"  FACTORY         : {oracle.FACTORY()}")
    print(f"  PROXY_IMPL      : {oracle.PROXY_IMPL()}")
    print("---------------- per-market price() proxies ----------------")
    for mid, lt, usd, asset, usd_price, asset_price in created:
        print(f"market {mid}  LT {lt}")
        print(f"    usd_oracle   : {usd}   price() = {usd_price}")
        print(f"    asset_oracle : {asset}   price() = {asset_price}")
    print("====================================================")
