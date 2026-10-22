#!/usr/bin/env python3

# Deployed at: 0xF3a2bf354C28a1e0C84033f3ba3379e223F56E6A

"""
Deploy LTDepositZap: deposits into a Yield Basis market with the debt solved on-chain, so they pass
the LT gate of a YBOraclizedPolicy (WETH market 10 after Curve vote 1508). No constructor
arguments, no privileges: nothing to vote on after deployment.

Usage (from the repo root):
  uv run python scripts/deploy_deposit_zap.py --fork   # deploy on a fork and deposit through it
  uv run python scripts/deploy_deposit_zap.py          # deploy from yb-deployer and verify on Etherscan
"""
import json
import os
import sys
from getpass import getpass
from time import sleep

import boa
from eth_account import account
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK, ETHERSCAN_API_KEY

FORK = "--fork" in sys.argv[1:]
EXTRA_TIMEOUT = 10
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer

# --fork only
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
MARKET_ID = 10                                          # yb-WETH
CURVE_POLICY = "0xAEa95d551154443E73565bE724d59D63d72f8A7d"  # YBOraclizedPolicy, set by Curve vote 1508
DEPOSIT = 10 * 10**18
WETH_ABI = json.dumps([
    {"name": "deposit", "stateMutability": "payable", "type": "function", "inputs": [], "outputs": []},
    {"name": "approve", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "address", "name": "spender"}, {"type": "uint256", "name": "amount"}], "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "address", "name": "owner"}], "outputs": [{"type": "uint256"}]},
])
ADMIN_ABI = '[{"name":"admin","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"}]'


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


def fork_check(zap):
    """Deposit WETH into market 10 through the zap, unstaked and staked, with the Curve policy attached"""
    market = boa.load_partial('contracts/Factory.vy').at(FACTORY).markets(MARKET_ID)
    lt = boa.load_partial('contracts/LT.vy').at(market.lt)
    amm = boa.load_partial('contracts/AMM.vy').at(market.amm)
    pool = boa.load_partial('contracts/twocrypto_pool/contracts/main/Twocrypto.vy').at(market.cryptopool)
    gauge = boa.loads_abi(WETH_ABI, name="Gauge").at(market.staker)
    weth = boa.loads_abi(WETH_ABI, name="WETH").at(WETH)

    if pool.POLICY() != CURVE_POLICY:
        print(f"Attaching {CURVE_POLICY} to {pool.address} as Curve vote 1508 would")
        with boa.env.prank(boa.loads_abi(ADMIN_ABI, name="CurveFactory").at(pool.factory()).admin()):
            pool.set_policy_contract(CURVE_POLICY)

    naive_debt = DEPOSIT * pool.price_oracle() // 10**18
    try:
        lt.preview_deposit(DEPOSIT, naive_debt)
        with boa.env.anchor():
            boa.env.set_balance(DEPLOYER, DEPOSIT + 10**18)
            weth.deposit(value=DEPOSIT)
            weth.approve(lt.address, 2**256 - 1)
            lt.deposit(DEPOSIT, naive_debt, 0)
        print("A price_oracle debt passes: no LT gate?")
    except boa.BoaError as e:
        assert "LT deviation" in str(e), e
        print("A price_oracle debt fails the LT gate, as expected")

    for stake in (False, True):
        boa.env.set_balance(DEPLOYER, DEPOSIT + 10**18)
        weth.deposit(value=DEPOSIT)
        weth.approve(zap.address, 2**256 - 1)
        debt = zap.calc_debt(lt.address, DEPOSIT)
        preview = zap.preview_deposit(lt.address, DEPOSIT, stake)
        debt_before, collateral_before = amm.get_debt(), amm.collateral_amount()
        shares = zap.deposit(lt.address, DEPOSIT, preview * 999 // 1000, DEPLOYER, stake)
        gas = zap._computation.get_gas_used()
        added_debt = amm.get_debt() - debt_before
        proportional = (amm.collateral_amount() - collateral_before) * debt_before // collateral_before
        name = "gauge shares" if stake else "LT shares"
        print(f"  stake={stake}: {DEPOSIT / 1e18} WETH, debt {debt / 1e18:,.2f} crvUSD -> {shares / 1e18:.6f} {name} "
              f"(preview {preview / 1e18:.6f}), off the book by {(added_debt - proportional) / proportional:+.1e}, gas {gas:,}")
        assert shares >= preview * 999 // 1000
        assert (gauge if stake else lt).balanceOf(DEPLOYER) >= shares
        assert abs(added_debt - proportional) * 10**6 <= proportional
    print("OK")


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = DEPLOYER
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)
        boa.env.add_account(account_load('yb-deployer'))

    zap = boa.load('contracts/utils/LTDepositZap.vy')
    print(f"LTDepositZap: {zap.address}")

    if FORK:
        fork_check(zap)
    else:
        verify(zap, etherscan, wait=True)
