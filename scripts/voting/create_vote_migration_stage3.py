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
NEW_MARKET_IDX = [3, 4, 5]
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
GAUGE_CONTROLLER = "0x1Be14811A3a06F6aF4fA64310a636e1Df04c1c21"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

USER = "0xeAfD26ffA47a9e387FB7409A456c4f7c4EF31ad8"

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
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    gauge_controller = boa.load_partial('contracts/dao/GaugeController.vy').at(GAUGE_CONTROLLER)
    lt_interface = boa.load_partial('contracts/LT.vy')
    erc20_interface = boa.load_abi(os.path.dirname(__file__) + '/erc20.abi.json')
    old_lts = [lt_interface.at(factory.markets(i).lt) for i in range(3)]
    new_lts = [lt_interface.at(factory.markets(i).lt) for i in NEW_MARKET_IDX]
    assets = [erc20_interface.at(lt.ASSET_TOKEN()) for lt in old_lts]
    factory_owner = boa.load('contracts/MigrationFactoryOwner.vy', DAO, FACTORY)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(factory_owner, etherscan, wait=True)
    migrator = boa.load('contracts/LTMigrator.vy', factory.STABLECOIN(), factory_owner.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(migrator, etherscan, wait=True)

    actions = []
    for i in NEW_MARKET_IDX:
        actions.append(
            Action(
                to=gauge_controller.address, value=0,
                data=gauge_controller.add_gauge.prepare_calldata(factory.markets(i).staker)
            )
        )
    actions.append(
        Action(
            to=factory.address, value=0,
            data=factory.set_admin.prepare_calldata(factory_owner.address, factory.emergency_admin())
        )
    )
    for old_lt, new_lt, asset in zip(old_lts, new_lts, assets):
        amount = old_lt.balanceOf(DAO)
        min_amount = 0
        if amount > 0:
            min_amount = int(0.98 * old_lt.preview_withdraw(amount))
        actions += [
            Action(
                to=old_lt.address, value=0,
                data=old_lt.withdraw.prepare_calldata(amount, min_amount)
            ),
            Action(
                to=factory_owner, value=0,
                data=factory_owner.lt_allocate_stablecoins.prepare_calldata(old_lt.address, 0)
            ),
            Action(
                to=asset, value=0,
                data=new_lt.approve.prepare_calldata(new_lt.address, 2**256 - 1)
            )
        ]

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Stage 3 of liquidity migration',
            'summary': 'Add gauges for new markets. Pass Factory to MigrationFactoryOwner. Withdraw wrapped Bitcoins from each market admin fees. Allocate freed up crvUSD to new markets. Approve Bitcoin wrappers for deposits into new markets.',
            'resources': []}).encode(),
        actions=actions,
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)
