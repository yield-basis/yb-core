#!/usr/bin/env python3

import boa
import os
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN


FORK = True
VOTING_PLUGIN = "0xD4f8EaCE89891e89FA46eE60B02a48D3d0FD137C"
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

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

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)

    yb = boa.load_abi(os.path.dirname(__file__) + '/erc20.abi.json', name="YB").at("0x766b660f3f3D5F97831FdF2F873235BbE100Cb30")

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({'title': 'Transfer', 'summary': 'send 1 yb', 'resources': []}).encode(),
        actions=[
            Action(to=voting.dao(), value=0,
                   data=yb.transfer.prepare_calldata("0x7a16fF8270133F063aAb6C9977183D9e72835428", 10**18))
        ],
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=1,
        tryEarlyExecution=True
    ))
    print(proposal_id)
