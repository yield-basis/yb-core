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
from time import sleep

from boa.explorer import Etherscan
from boa.verifiers import verify


FORK = False
VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

EXTRA_TIMEOUT = 60 * 2
ETHERSCAN_URL = "https://api.etherscan.io/api"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])


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
        etherscan = Etherscan(ETHERSCAN_URL, ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_abi(os.path.dirname(__file__) + '/Factory.abi.json', name="YB").at("0x370a449FeBb9411c95bf897021377fe0B7D100c0")

    amm_interface = boa.load_partial('contracts/AMM.vy')
    yb_amm_impl = amm_interface.deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        yb_amm_impl.ctor_calldata = b""
        verify(yb_amm_impl, etherscan, wait=True)
    lt_interface = boa.load_partial('contracts/LT.vy')
    yb_lt_impl = lt_interface.deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        yb_lt_impl.ctor_calldata = b""
        verify(yb_lt_impl, etherscan, wait=True)
    gauge_impl = boa.load_partial('contracts/dao/LiquidityGauge.vy').deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        gauge_impl.ctor_calldata = b""
        verify(gauge_impl, etherscan, wait=True)

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Use new AMM, LT and Gauge implementations in Yield Basis',
            'summary': 'Implementations contain Medium and Low fixes of issues reported by Chainsecurity, Statemind and Sherlocks',
            'resources': []}).encode(),
        actions=[
            Action(to=factory.address, value=0,
                   data=factory.set_implementations.prepare_calldata(
                       yb_amm_impl.address,
                       yb_lt_impl.address,
                       factory.virtual_pool_impl(),
                       factory.price_oracle_impl(),
                       gauge_impl.address
                       )
                   )
        ],
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)
