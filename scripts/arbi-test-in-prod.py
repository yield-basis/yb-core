#!/usr/bin/env python3

import boa
import os
import json
from getpass import getpass
from eth_account import account
from collections import namedtuple
from boa.explorer import Etherscan
from boa.contracts.vyper.vyper_contract import VyperBlueprint

from keys import ARBISCAN_KEY
from keys import ARBITRUM_NETWORK as NETWORK


Market = namedtuple('Market', ['asset', 'cryptopool', 'amm', 'lt', 'price_oracle', 'virtual_pool', 'staker'])


ARBISCAN_URL = "https://api.arbiscan.io/api"

YB_MULTISIG = "0xd396db54cAB0eCB51d43e82f71adc0B70a077aAF"
BTC_TOKEN = "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"  # WBTC on arbitrum
USD_TOKEN = "0x498Bf2B1e120FeD3ad3D42EA2165E9b73f99C1e5"  # crvUSD on arbutrum
AGG = "0x44a4FdFb626Ce98e36396d491833606309520330"
FLASH = "0x0B68dBC2DE05448A195ea80BCe6356076ADca981"

POOL_FOR_ORACLE = "0x82670f35306253222F8a165869B28c64739ac62e"

REDUCE_SIZE = 1000


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


VyperBlueprint.ctor_calldata = b''  # Hack to make boa verify blueprints


if __name__ == '__main__':
    verifier = Etherscan(ARBISCAN_URL, ARBISCAN_KEY)
    boa.set_network_env(NETWORK)
    deployer = account_load('yb-deployer')
    boa.env.add_account(deployer)
    boa.env._fork_try_prefetch_state = False

    erc20_compiled = boa.load_partial("contracts/testing/ERC20Mock.vy")
    btc = erc20_compiled.at(BTC_TOKEN)
    usd = erc20_compiled.at(USD_TOKEN)

    pool_for_oracle = boa.from_etherscan(POOL_FOR_ORACLE, name="3crypto", uri=ARBISCAN_URL, api_key=ARBISCAN_KEY)
    price_oracle = pool_for_oracle.price_oracle(0)
    print("Price:", price_oracle / 1e18)

    print("== Deploying Twocrypto ==")
    amm_interface = boa.load_partial('contracts/testing/twocrypto/Twocrypto.vy')
    amm_impl = amm_interface.deploy_as_blueprint()
    boa.verify(amm_impl, verifier)
    math_impl = boa.load('contracts/testing/twocrypto/StableswapMath.vy')
    boa.verify(math_impl, verifier)
    views_impl = boa.load('contracts/testing/twocrypto/TwocryptoView.vy')
    boa.verify(views_impl, verifier)
    gauge_impl = "0x0000000000000000000000000000000000000000"

    factory = boa.load('contracts/testing/twocrypto/TwocryptoFactory.vy')
    boa.verify(factory, verifier)
    factory.initialise_ownership(YB_MULTISIG, deployer)  # fee_receiver, admin
    factory.set_pool_implementation(amm_impl, 0)
    factory.set_gauge_implementation(gauge_impl)
    factory.set_views_implementation(views_impl)
    factory.set_math_implementation(math_impl)

    # Params have nothing to do with reality!
    pool = amm_interface.at(
        factory.deploy_pool(
            "Test WBTC/crvUSD pool",  # _name: String[64]
            "TST",  # _symbol: String[32]
            [usd.address, btc.address],
            0,  # implementation_id: uint256
            int(15.68 * 10000),  # A: uint256
            int(1e-5 * 1e18),           # gamma: uint256 <- does not matter with stableswap
            int(0.003 * 1e10),          # mid_fee: uint256
            int(0.0227 * 1e10),         # out_fee: uint256
            int(0.196 * 1e18),          # fee_gamma: uint256
            int(1e-10 * 1e18),          # allowed_extra_profit: uint256
            int(1e-6 * 1e18),           # adjustment_step: uint256
            866,                        # ma_exp_time: uint256
            price_oracle                # initial_price: uint256
        ))
    pool.set_admin_fee(0)

    factory.commit_transfer_ownership(YB_MULTISIG)  # New owner must accept!

    print("== Deploying YB ==")
    amm_interface = boa.load_partial('contracts/AMM.vy')
    yb_amm_impl = amm_interface.deploy_as_blueprint()
    boa.verify(yb_amm_impl, verifier)
    lt_interface = boa.load_partial('contracts/LT-Restricted.vy')
    yb_lt_impl = lt_interface.deploy_as_blueprint()
    boa.verify(yb_lt_impl, verifier)
    vpool_impl = boa.load_partial('contracts/VirtualPool.vy').deploy_as_blueprint()
    boa.verify(vpool_impl, verifier)
    oracle_impl = boa.load_partial('contracts/CryptopoolLPOracle.vy').deploy_as_blueprint()
    boa.verify(oracle_impl, verifier)
    gauge_impl = "0x0000000000000000000000000000000000000000"
    # agg
    # flash
    fee_receiver = YB_MULTISIG
    factory_admin = deployer
    emergency_admin = YB_MULTISIG

    yb_factory = boa.load(
        'contracts/Factory.vy',
        USD_TOKEN,
        yb_amm_impl,
        yb_lt_impl,
        vpool_impl,
        oracle_impl,
        gauge_impl,
        AGG,
        FLASH,
        fee_receiver,
        factory_admin,
        emergency_admin)
    boa.verify(yb_factory, verifier)

    print("== Seed Twocrypto ==")
    # Seed liqudiity with 2 dollars
    btc.approve(pool.address, 2**256-1)
    usd.approve(pool.address, 2**256-1)
    # $400 seed
    pool.add_liquidity([200 * 10**18 // REDUCE_SIZE, int(200 * 10**8 / (price_oracle / 1e18)) // REDUCE_SIZE], 0)

    print("== Creating market ==")
    # Get stables for factory
    usd.approve(yb_factory.address, 2**256-1)
    yb_factory.set_allocator(deployer, 50_000 * 10**18 // REDUCE_SIZE)

    # Create market
    yb_fee = int(0.0085 * 1e18)
    yb_rate = int(0.187 * 2 / (365 * 86400) * 1e18)
    yb_factory.add_market(pool.address, yb_fee, yb_rate, 50_000 * 10**18 // REDUCE_SIZE)
    market = Market(*yb_factory.markets(0))

    # Set admin to msig
    yb_factory.set_admin(YB_MULTISIG, YB_MULTISIG)

    print(f"Factory: {yb_factory.address}")
    print(f"Pool:    {market.cryptopool}")
    print(f"AMM:     {market.amm}")
    print(f"LT:      {market.lt}")
    print(f"VPool:   {market.virtual_pool}")
