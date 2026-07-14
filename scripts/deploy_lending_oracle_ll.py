#!/usr/bin/env python3
"""
Deploy a YBLendingOracleLL (EMA-smoothed ybLT/asset oracle) for a single LT.

One LL is deployed per market. The LT is resolved from the factory market id (or set
LT_ADDRESS to override). cached_price is unseeded until the first price_w().

    FORK = True   -> deploy on a fork and sanity-check price() / price_w().
    FORK = False  -> broadcast on mainnet and verify on Etherscan.

    python scripts/deploy_lending_oracle_ll.py
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
MARKET_ID = 7                                            # market whose LT this LL prices
LT_ADDRESS = ""                                          # override: use this LT directly if set


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

    lt = LT_ADDRESS
    if not lt:
        lt = boa.load_partial('contracts/Factory.vy').at(FACTORY).markets(MARKET_ID).lt
    print(f"LT (market {MARKET_ID}): {lt}")

    ll = boa.load('contracts/utils/YBLendingOracleLL.vy', lt)
    if not FORK:
        verify(ll, etherscan, wait=True)
    print(f"YBLendingOracleLL: {ll.address}")
    print(f"  LT_TOKEN: {ll.LT_TOKEN()}")

    if FORK:
        raw = ll.price()          # unseeded EMA returns the raw price
        seeded = ll.price_w()     # seeds + checkpoints the EMA
        assert raw > 0 and seeded > 0, "zero price"
        assert ll.cached_price() == seeded, "EMA not seeded"
        assert ll.price() == seeded, "settled EMA of a constant != the constant"
        print(f"  price():   {raw}")
        print(f"  price_w(): {seeded}")
        print("price() / price_w() seed and match - OK")
