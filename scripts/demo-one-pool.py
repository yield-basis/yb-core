#!/usr/bin/env python3

import boa
import sys
import json
from time import sleep
import subprocess
from eth_account import account
from boa.network import ExternalAccount
from collections import namedtuple


Market = namedtuple('Market', ['asset', 'cryptopool', 'amm', 'lt', 'price_oracle', 'virtual_pool', 'staker'])


NETWORK = "http://localhost:8545"
HARDHAT_COMMAND = ["npx", "hardhat", "node", "--fork", "https://eth.drpc.org", "--port", "8545"]
demo_user_address = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
demo_user_key = bytes.fromhex("ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")

BTC_TOKEN = "0x18084fba666a33d37592fa2633fd49a74dd93a88"
USD_TOKEN = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
TEST_RESERVE = "0x2889302a794da87fbf1d6db415c1492194663d13"
AGG = "0x18672b1b0c623a30089A280Ed9256379fb0E4E62"
FLASH = "0xC9332fdCB1C491Dcc683bAe86Fe3cb70360738BC"


if __name__ == '__main__':
    if '--hardhat' in sys.argv[1:]:
        hardhat = subprocess.Popen(HARDHAT_COMMAND)
        sleep(10)

    boa.set_network_env(NETWORK)
    boa.env._fork_try_prefetch_state = False

    erc20_compiled = boa.load_partial("contracts/testing/ERC20Mock.vy")
    btc = erc20_compiled.at(BTC_TOKEN)
    usd = erc20_compiled.at(USD_TOKEN)

    boa.env.add_account(account.Account.from_key(demo_user_key))
    admin = demo_user_address  # Doesn't have to be

    boa.env._rpc.fetch("hardhat_impersonateAccount", [TEST_RESERVE])
    boa.env.add_account(ExternalAccount(_rpc=boa.env._rpc, address=TEST_RESERVE))
    boa.env._rpc.fetch("hardhat_setBalance", [TEST_RESERVE, "0x1000000000000000000"])
    with boa.env.prank(TEST_RESERVE):
        btc.transfer(demo_user_address, 10**18)
        usd.transfer(demo_user_address, 10**18)
        usd.transfer(admin, 200_000 * 10**18)

    with boa.env.prank(admin):
        amm_interface = boa.load_partial('contracts/testing/twocrypto/Twocrypto.vy')
        amm_impl = amm_interface.deploy_as_blueprint()
        math_impl = boa.load('contracts/testing/twocrypto/StableswapMath.vy')
        views_impl = boa.load('contracts/testing/twocrypto/TwocryptoView.vy')
        gauge_impl = "0x0000000000000000000000000000000000000000"

        factory = boa.load('contracts/testing/twocrypto/TwocryptoFactory.vy')
        factory.initialise_ownership(admin, admin)
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
                600,                        # ma_exp_time: uint256
                100_000 * 10**18            # initial_price: uint256  XXX
            ))
        pool.set_admin_fee(0)

        amm_interface = boa.load_partial('contracts/AMM.vy')
        yb_amm_impl = amm_interface.deploy_as_blueprint()
        lt_interface = boa.load_partial('contracts/LT.vy')
        yb_lt_impl = lt_interface.deploy_as_blueprint()
        vpool_impl = boa.load_partial('contracts/VirtualPool.vy').deploy_as_blueprint()
        oracle_impl = boa.load_partial('contracts/CryptopoolLPOracle.vy').deploy_as_blueprint()
        gauge_impl = "0x0000000000000000000000000000000000000000"
        # agg
        # flash
        fee_receiver = "0x0000000000000000000000000000000000000000"

        factory = boa.load(
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
            admin,
            admin)

        # Seed liqudiity with 2 dollars
        btc.approve(pool.address, 2**256-1)
        usd.approve(pool.address, 2**256-1)
        pool.add_liquidity([10**18, 10**18 // 100_000], 0)

        # Get stables for factory
        usd.approve(factory.address, 2**256-1)
        factory.set_allocator(admin, 200_000 * 10**18)

        # Create market
        factory.add_market(pool.address, int(0.01 * 1e18), int(0.1 / (365 * 86400) * 1e18), 200_000 * 10**18)
        market = Market(*factory.markets(0))

    print(f"Factory: {factory.address}")
    print(f"Pool:    {market.cryptopool}")
    print(f"AMM:     {market.amm}")
    print(f"LT:      {market.lt}")
    print(f"VPool:   {market.virtual_pool}")

    yb_amm = amm_interface.at(market.amm)
    yb_lt = lt_interface.at(market.lt)

    with open('factory_abi.json', 'w') as f:
        json.dump(factory.abi, f)

    with open('pool_abi.json', 'w') as f:
        json.dump(pool.abi, f)

    with open('amm_abi.json', 'w') as f:
        json.dump(yb_amm.abi, f)

    with open('lt_abi.json', 'w') as f:
        json.dump(yb_lt.abi, f)

    if '--deposit' in sys.argv[1:]:
        print('Simulating deposit')
        btc.approve(yb_lt.address, 2**256-1)
        yb_lt.deposit(int(0.5e18), int(50_000e18), 0)
        print('Deposited')

    if '--hardhat' in sys.argv[1:]:
        hardhat.wait()
