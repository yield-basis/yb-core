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
        usd, asset = ll_factory.create_oracles(mid)
        usd_o = ll.at(usd)
        asset_o = ll.at(asset)
        usd_price = usd_o.price()
        asset_price = asset_o.price()
        pps = lt_d.at(lt).pricePerShare()          # redemption coefficient (fair value/share)
        if FORK:
            # Clones must be wired correctly and creation is idempotent.
            assert usd_o.lt_token() == lt and asset_o.lt_token() == lt, f"market {mid} wrong LT"
            assert usd_o.in_usd() and not asset_o.in_usd(), f"market {mid} denom"
            assert usd_o.factory() == ll_factory.address, f"market {mid} factory"
            assert usd_o.ema_time() == EMA_TIME and asset_o.ema_time() == EMA_TIME, f"market {mid} ema"
            assert ll_factory.create_oracles(mid) == (usd, asset), f"market {mid} not idempotent"
            # price_w seeds the EMA; a constant settles to itself.
            assert usd_o.price_w() > 0 and asset_o.price_w() > 0, f"market {mid} zero price"
        created.append((mid, lt, usd, asset, usd_price, asset_price, pps))
        print(f"market {mid}: created EMA oracles")

    print("\n==================== deployment ====================")
    print(f"YBLendingOracleLL impl   : {impl.address}")
    print(f"YBLendingOracleLLFactory : {ll_factory.address}")
    print(f"  FACTORY                : {ll_factory.FACTORY()}")
    print(f"  LL_IMPL                : {ll_factory.LL_IMPL()}")
    print(f"  dao                    : {ll_factory.dao()}")
    print(f"  default_ema_time       : {ll_factory.default_ema_time()} s")
    print("---------------- per-market EMA oracles ----------------")
    for mid, lt, usd, asset, usd_price, asset_price, pps in created:
        print(f"market {mid}  LT {lt}")
        print(f"    usd_oracle    : {usd}   price() = {usd_price/1e18:.2f}")
        print(f"    asset_oracle  : {asset}   price() = {asset_price/1e18:.4f}")
        print(f"    pricePerShare : {pps/1e18:.6f}")
    print("====================================================")
