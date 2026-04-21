#!/usr/bin/env python3

import boa
import os
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import ETHERSCAN_API_KEY
from networks import PINATA_TOKEN
from time import sleep, time
from vyper.utils import method_id

from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify


MARKETS = [3, 4, 5]

FEE = int(0.03 * 10**18)   # 3%

DEADLINE = 3 * 7 * 86400       # 3 weeks from now

FORK = True
EXTRA_TIMEOUT = 10

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CALL_COMPARATOR = "0xd3BFa85dc668Aab38121bE12D69dd180301dec25"

USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])

PRICE_SCALE_SELECTOR = method_id("price_scale()")
FEE_SELECTOR = method_id("fee()")


def pin_to_ipfs(content: dict):
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {
        "Authorization": f"Bearer {PINATA_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "pinataContent": content,
        "pinataMetadata": {"name": "pinnie.json"},
        "pinataOptions": {"cidVersion": 1},
    }

    response = requests.request("POST", url, json=payload, headers=headers)
    assert 200 <= response.status_code < 400

    return 'ipfs://' + response.json()["IpfsHash"]


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


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK)
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load_partial('contracts/MigrationFactoryOwner.vy').at(factory.admin())
    comparator = boa.load_partial('contracts/dao/CallComparator.vy').at(CALL_COMPARATOR)
    twocrypto_deployer = boa.load_partial('contracts/twocrypto_ng/contracts/main/Twocrypto.vy')

    now = int(time())
    deadline = now + DEADLINE

    # Gather current market state
    market_data = []
    for market_id in MARKETS:
        market = factory.markets(market_id)
        lt = boa.load_partial('contracts/LT.vy').at(market.lt)
        amm = boa.load_partial('contracts/AMM.vy').at(market.amm)
        cryptopool = twocrypto_deployer.at(market.cryptopool)
        current_fee = amm.fee()
        current_price_scale = cryptopool.price_scale()

        market_data.append({
            'market_id': market_id,
            'lt': lt,
            'amm': amm,
            'cryptopool': cryptopool,
            'current_fee': current_fee,
            'current_price_scale': current_price_scale,
        })
        print(f"Market {market_id}: fee={current_fee / 1e18:.4%}, price_scale={current_price_scale}")

    # --- Vote A: Set fee to 3% if timestamp < deadline AND price_scale == current ---
    actions_a = []
    actions_a.append(Action(to=comparator.address, value=0,
                            data=comparator.check_called_after.prepare_calldata(60)))
    actions_a.append(Action(to=comparator.address, value=0,
                            data=comparator.check_timestamp_lt.prepare_calldata(deadline)))
    for m in market_data:
        actions_a.append(Action(to=comparator.address, value=0,
                                data=comparator.check_equal.prepare_calldata(m['cryptopool'].address,
                                                                             PRICE_SCALE_SELECTOR,
                                                                             m['current_price_scale'])))
        actions_a.append(Action(to=factory_owner.address, value=0,
                                data=factory_owner.lt_set_amm_rate.prepare_calldata(m['lt'].address, FEE)))

    # --- Vote B: Restore original fee if timestamp < deadline AND price_scale != current AND fee == 3% ---
    actions_b = []
    actions_b.append(Action(to=comparator.address, value=0,
                            data=comparator.check_called_after.prepare_calldata(60)))
    actions_b.append(Action(to=comparator.address, value=0,
                            data=comparator.check_timestamp_lt.prepare_calldata(deadline)))
    for m in market_data:
        actions_b.append(Action(to=comparator.address, value=0,
                                data=comparator.check_nonequal.prepare_calldata(m['cryptopool'].address,
                                                                                PRICE_SCALE_SELECTOR,
                                                                                m['current_price_scale'])))
        actions_b.append(Action(to=comparator.address, value=0,
                                data=comparator.check_equal.prepare_calldata(m['amm'].address, FEE_SELECTOR, FEE)))
        actions_b.append(Action(to=factory_owner.address, value=0,
                                data=factory_owner.lt_set_amm_rate.prepare_calldata(m['lt'].address, m['current_fee'])))

    all_votes = [
        ('A', actions_a, 'Raise BTC market fees to 3% (price_scale unchanged)',
         'Set AMM fee to 3% for WBTC, cbBTC, tBTC markets. '
         'Guarded: only executes if price_scale has not changed and within 3 weeks.'),
        ('B', actions_b, 'Restore BTC market fees to original (price_scale changed)',
         'Restore original AMM fee for BTC markets after price_scale change. '
         'Guarded: only executes if price_scale changed, fee is 3%, and within 3 weeks.'),
    ]

    for i, (label, actions, title, summary) in enumerate(all_votes):
        if not FORK:
            if i > 0:
                acc = account_load('yb-deployer-a')
                boa.env.add_account(acc, force_eoa=True)
            proposal_id = voting.createProposal(*Proposal(
                metadata=pin_to_ipfs({
                    'title': title,
                    'summary': summary,
                    'resources': []}).encode(),
                actions=actions,
                allowFailureMap=0,
                startDate=0,
                endDate=0,
                voteOption=0,
                tryEarlyExecution=True
            ))
            print(f"Vote {label} proposal ID: {proposal_id}")

    if FORK:
        print("\n=== Simulating Vote A (fee -> 3%, price_scale unchanged) ===")
        with boa.env.prank(DAO):
            for action in actions_a:
                boa.env.raw_call(to_address=action.to, data=action.data)
        for m in market_data:
            print(f"  Market {m['market_id']} fee: {m['amm'].fee() / 1e18:.4%}")

        print("\n=== Vote B should revert (price_scale hasn't changed) ===")
        try:
            with boa.env.prank(DAO):
                for action in actions_b:
                    boa.env.raw_call(to_address=action.to, data=action.data)
            print("  ERROR: Vote B should have reverted!")
        except Exception as e:
            print(f"  Correctly reverted: {e}")
