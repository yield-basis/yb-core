#!/usr/bin/env python3

import boa
import sys
import json
from time import sleep
import subprocess
from eth_account import account
from boa.network import ExternalAccount


NETWORK = "http://localhost:8545"
HARDHAT_COMMAND = ["npx", "hardhat", "node", "--fork", "https://eth.drpc.org", "--port", "8545"]
demo_user_address = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
demo_user_key = bytes.fromhex("ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")

BTC_TOKEN = "0x18084fba666a33d37592fa2633fd49a74dd93a88"
USD_TOKEN = "0xf939e0a03fb07f59a73314e73794be0e57ac1b4e"
TEST_RESERVE = "0x2889302a794da87fbf1d6db415c1492194663d13"


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
        amm_interface = boa.load_partial('contracts/twocrypto/CurveTwocryptoOptimized.vy')
        amm_impl = amm_interface.deploy_as_blueprint()
        math_impl = boa.load('contracts/twocrypto/CurveCryptoMathOptimized2.vy')
        views_impl = boa.load('contracts/twocrypto/CurveCryptoViews2Optimized.vy')
        gauge_impl = "0x0000000000000000000000000000000000000000"

        factory = boa.load('contracts/twocrypto/CurveTwocryptoFactory.vy')
        factory.initialise_ownership(admin, admin)
        factory.set_pool_implementation(amm_impl, 0)
        factory.set_gauge_implementation(gauge_impl)
        factory.set_views_implementation(views_impl)
        factory.set_math_implementation(math_impl)

        # Params have nothing to do with reality!
        pool = amm_interface.at(
            factory.deploy_pool(
                "Test pool",  # _name: String[64]
                "TST",  # _symbol: String[32]
                [usd.address, btc.address],
                0,  # implementation_id: uint256
                5 * 10000 * 2**2,  # A: uint256
                int(1e-5 * 1e18),  # gamma: uint256
                int(0.0025 * 1e10),  # mid_fee: uint256
                int(0.0045 * 1e10),  # out_fee: uint256
                int(0.01 * 1e18),  # fee_gamma: uint256
                int(1e-10 * 1e18),  # allowed_extra_profit: uint256
                int(1e-6 * 1e18),  # adjustment_step: uint256
                600,  # ma_exp_time: uint256
                100_000 * 10**18  # initial_price: uint256
            ))

        cryptopool_oracle = boa.load('contracts/CryptopoolLPOracle.vy', pool.address)

        lt = boa.load(
            'contracts/LT.vy',
            btc.address,
            usd.address,
            pool.address,
            admin)

        amm = boa.load(
            'contracts/AMM.vy',
            lt.address,
            usd.address,
            pool.address,
            2 * 10**18,  # leverage = 2.0
            int(0.007e18),  # fee
            cryptopool_oracle.address
        )
        lt.set_amm(amm.address)

        # Seed liqudiity with 2 dollars
        btc.approve(pool.address, 2**256-1)
        usd.approve(pool.address, 2**256-1)
        pool.add_liquidity([10**18, 10**18 // 100_000], 0)

        usd.approve(lt.address, 2**256-1)
        lt.allocate_stablecoins(admin, 200_000 * 10**18)

    print(f"Pool:   {pool.address}")
    print(f"AMM:    {amm.address}")
    print(f"LT:     {lt.address}")

    with open('pool_abi.json', 'w') as f:
        json.dump(pool.abi, f)

    with open('amm_abi.json', 'w') as f:
        json.dump(amm.abi, f)

    with open('lt_abi.json', 'w') as f:
        json.dump(lt.abi, f)

    if '--deposit' in sys.argv[1:]:
        print('Simulating deposit')
        btc.approve(lt.address, 2**256-1)
        lt.deposit(int(0.5e18), int(50_000e18), 0)
        print('Deposited')

    if '--hardhat' in sys.argv[1:]:
        hardhat.wait()
