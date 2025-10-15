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
from boa.verifiers import verify as boa_verify


FORK = False
VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
USER = "0xeAfD26ffA47a9e387FB7409A456c4f7c4EF31ad8"
YB = "0x01791F726B4103694969820be083196cC7c045fF"
SPLITTER = "0xC8aA884f54eA5eaEF5ef4086607C00580a103928"

EXTRA_TIMEOUT = 10


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
        USER = account_load('yb-deployer-2')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_abi(os.path.dirname(__file__) + '/Factory.abi.json', name="Factory").at("0x370a449FeBb9411c95bf897021377fe0B7D100c0")
    yb = boa.load_partial('contracts/dao/YB.vy').at(YB)

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Send tokens for Curve governance airdrop to veCRV voters',
            'summary': 'Allocate 5M YB for Curve ve-governance airdrop - those who voted YES for important proposals 1206, 1213 and 1222',
            'resources': []}).encode(),
        actions=[
            Action(to=YB, value=0, data=yb.transfer.prepare_calldata(SPLITTER, 5 * 10**6 * 10**18))
        ],
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)
