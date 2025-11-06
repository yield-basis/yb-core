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


FORK = True
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
GAUGE_CONTROLLER = "0x1Be14811A3a06F6aF4fA64310a636e1Df04c1c21"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

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
    lt_interface = boa.load_partial('contracts/LT.vy')
    lts = [lt_interface.at(factory.markets(i).lt) for i in range(3)]

    lt_blueprint = lt_interface.deploy_as_blueprint()
    if not FORK:
        lt_blueprint.ctor_calldata = b""
        verify(lt_blueprint, etherscan, wait=True)

    gauge_interface = boa.load_partial('contracts/dao/LiquidityGauge.vy')
    gauge_blueprint = gauge_interface.deploy_as_blueprint()
    if not FORK:
        lt_blueprint.ctor_calldata = b""
        verify(gauge_blueprint, etherscan, wait=True)

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Stage 1 of liquidity migration',
            'summary': 'Set new LT implementation. Set fee receiver to the DAO. Withdraw all admin fees to the DAO.',
            'resources': []}).encode(),
        actions=[
            Action(
                to=factory.address, value=0,
                data=factory.set_implementations.prepare_calldata(
                    ZERO_ADDRESS, lt_blueprint.address, ZERO_ADDRESS, ZERO_ADDRESS, gauge_blueprint.address
                )
            ),
            Action(
                to=factory.address, value=0,
                data=factory.set_fee_receiver.prepare_calldata(DAO)
            ),
        ] + [
            Action(
                to=lt.address, value=0,
                data=lt.withdraw_admin_fees.prepare_calldata()
            )
            for lt in lts
        ],
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)
