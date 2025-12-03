#!/usr/bin/env python3

import boa
import os
import json
import requests

from time import sleep
from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify
from networks import ETHERSCAN_API_KEY


FORK = True
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
FEE_DISTRIBUTOR = "0xD11b416573EbC59b6B2387DA0D2c0D1b3b1F7A90"

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
    fee_distributor = boa.load_partial('contracts/dao/FeeDistributor.vy').at(FEE_DISTRIBUTOR)
    lt_interface = boa.load_partial('contracts/LT.vy')
    lts = [lt_interface.at(factory.markets(i).lt) for i in [3, 4, 5]]

    assert fee_distributor.owner() == DAO
    token_set_id = fee_distributor.current_token_set()
    token_set = set([fee_distributor.token_sets(token_set_id, i) for i in range(6)])

    token_sender = boa.load(
            'contracts/dao/TokenSender.vy', FEE_DISTRIBUTOR,
            [lt.address for lt in lts] + [lt.ASSET_TOKEN() for lt in lts]
    )
    if not FORK:
        verify(token_sender, etherscan, wait=True)

    erc20 = boa.load_abi(os.path.dirname(__file__) + '/erc20.abi.json')

    actions = []

    for lt in lts:
        actions.append(
            Action(
                to=lt.address, value=0,
                data=lt.withdraw_admin_fees.prepare_calldata()
            ))
        actions.append(
            Action(
                to=lt.address, value=0,
                data=lt.approve.prepare_calldata(token_sender.address, 2**256 - 1)
            ))
        token = erc20.at(lt.ASSET_TOKEN())
        actions.append(
            Action(
                to=token.address, value=0,
                data=token.approve.prepare_calldata(token_sender.address, 2**256 - 1)
            ))
        assert lt.address in token_set
        assert token.address in token_set

    actions.append(
        Action(
            to=factory_owner.address, value=0,
            data=factory_owner.set_fee_receiver.prepare_calldata(FEE_DISTRIBUTOR)
        ))

    actions.append(
        Action(
            to=token_sender.address, value=0,
            data=token_sender.send.prepare_calldata()
        ))

    for t in list(token_set):
        token = erc20.at(t)
        actions.append(
            Action(
                to=token.address, value=0,
                data=token.approve.prepare_calldata(token_sender.address, 0)
            ))

    actions.append(
        Action(
            to=fee_distributor.address, value=0,
            data=fee_distributor.fill_epochs.prepare_calldata()
        ))

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs({
            'title': 'Turn fee distribution ON',
            'summary': 'Change fee distributor, claim admin fees from new markets, distibute all LT tokens for new markets and wrapped BTC tokens sitting in DAO',  # noqa
            'resources': []}).encode(),
        actions=actions,
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True
    ))
    print(proposal_id)

    if FORK:
        print("Simulating execution")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                print(i + 1, 'out of', len(actions))
                boa.env.raw_call(to_address=action.to, data=action.data)

        print("Values after execution:")
        print(f"Fee receiver is set to {factory.fee_receiver()} which is the same as {FEE_DISTRIBUTOR}")
        print(f"Token balances in fee receiver:")
        tokens = [erc20.at(t) for t in list(token_set)]
        for token in tokens:
            print(f'  - {token.symbol()}: {token.balanceOf(FEE_DISTRIBUTOR) / 10**token.decimals()}')

        print("Claim after time travel")
        test_user = "0x7a16fF8270133F063aAb6C9977183D9e72835428"
        print("Before")
        print("User balances")
        for token in tokens:
            print(token.address, token.balanceOf(test_user))
        with boa.env.prank(test_user):
            ttokens, amounts = fee_distributor.preview_claim(test_user, 50, True)
            for t, a in zip(ttokens, amounts):
                print(t, a)
        boa.env.time_travel(7 * 86400)
        print("After")
        with boa.env.prank(test_user):
            ttokens, amounts = fee_distributor.preview_claim(test_user, 50, True)
            for t, a in zip(ttokens, amounts):
                print(t, a)
        print("User balances")
        for token in tokens:
            print(token.address, token.balanceOf(test_user))
