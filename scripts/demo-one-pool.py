#!/usr/bin/env python3

import boa
import sys
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

    boa.env._rpc.fetch("hardhat_impersonateAccount", [TEST_RESERVE])
    boa.env.add_account(ExternalAccount(_rpc=boa.env._rpc, address=TEST_RESERVE))
    boa.env._rpc.fetch("hardhat_setBalance", [TEST_RESERVE, "0x1000000000000000000"])
    btc.transfer(demo_user_address, 10**18, sender=TEST_RESERVE)
    usd.transfer(demo_user_address, 10**18, sender=TEST_RESERVE)

    if '--hardhat' in sys.argv[1:]:
        hardhat.wait()
