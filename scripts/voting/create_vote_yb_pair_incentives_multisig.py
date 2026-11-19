#!/usr/bin/env python3
"""
Create a DAO vote which transfers 2.5M YB from the DAO to the multisig which manages incentives for the
YB/crvUSD pair on Curve (or other YB pairs if ever needed).

The YB comes out of the 12.5M "YB/crvUSD pair rewards" allocation: Vest 1yi
0x1cb8f667De695f3d126534d9808567F299B90a86 vested it to the DAO (fully vested 2026-09-15) and the unclaimed
remainder has been claimed to the DAO. Only the DAO can move it from there, hence the vote.

The recipient is a Safe; the script prints its version, owners and threshold from chain before proposing.

Fork mode runs the vote's actions as the DAO on a fork at head and checks the balances moved.

    python scripts/voting/create_vote_yb_pair_incentives_multisig.py           # create the vote
    python scripts/voting/create_vote_yb_pair_incentives_multisig.py --fork    # simulate on a fork
"""

import boa
import os
import sys
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN


FORK = "--fork" in sys.argv[1:]

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
YB = "0x01791F726B4103694969820be083196cC7c045fF"
PAIR_REWARDS_VEST = "0x1cb8f667De695f3d126534d9808567F299B90a86"  # Vest 1yi, "YB/crvUSD pair rewards"
INCENTIVES_MULTISIG = "0xdB1335Afe040ef079246baDB34Dc97E6e3b0e11b"
AMOUNT = 2_500_000 * 10**18

# Whale with voting power, used only as the caller in fork mode.
# In production the proposal is created by the yb-deployer account.
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])

SAFE_ABI = json.dumps([
    {"name": "getOwners", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address[]"}]},
    {"name": "getThreshold", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "VERSION", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "string"}]},
])


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
        pkey = account.Account.decrypt(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    yb = boa.load_partial('contracts/dao/YB.vy').at(YB)
    safe = boa.loads_abi(SAFE_ABI, name="Safe").at(INCENTIVES_MULTISIG)

    dao_balance = yb.balanceOf(DAO)
    print(f"DAO YB balance:      {dao_balance / 1e18:,.2f}")
    print(f"Transfer:            {AMOUNT / 1e18:,.2f} YB -> {INCENTIVES_MULTISIG}")
    print(f"Multisig:            Safe {safe.VERSION()}, {safe.getThreshold()} of {len(safe.getOwners())}")
    for owner in safe.getOwners():
        print(f"  owner {owner}")
    assert dao_balance >= AMOUNT, "DAO does not hold enough YB (claim the vest to the DAO first)"

    actions = [
        Action(to=YB, value=0, data=yb.transfer.prepare_calldata(INCENTIVES_MULTISIG, AMOUNT)),
    ]

    title = 'Move 2.5M YB to a multisig managing incentives for the YB/crvUSD pair'
    summary = (
        f'Transfer 2,500,000 YB from the DAO to the multisig {INCENTIVES_MULTISIG}, which will manage incentives '
        'for the YB/crvUSD pool on Curve (or other YB pairs if ever needed). The YB comes from the 12.5M '
        f'"YB/crvUSD pair rewards" allocation, vested to the DAO through {PAIR_REWARDS_VEST} and claimed to the DAO.'
    )
    print(f"\nTitle:   {title}\nSummary: {summary}\n")

    if not FORK:
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
        print("Proposal ID:", proposal_id)

    else:
        multisig_balance = yb.balanceOf(INCENTIVES_MULTISIG)
        print("Simulating execution (DAO runs the proposal actions)")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                boa.env.raw_call(to_address=action.to, data=action.data)
                print(f"  action {i + 1} out of {len(actions)}")

        print(f"  DAO YB:      {dao_balance / 1e18:,.2f} -> {yb.balanceOf(DAO) / 1e18:,.2f}")
        print(f"  multisig YB: {multisig_balance / 1e18:,.2f} -> {yb.balanceOf(INCENTIVES_MULTISIG) / 1e18:,.2f}")
        assert yb.balanceOf(DAO) == dao_balance - AMOUNT
        assert yb.balanceOf(INCENTIVES_MULTISIG) == multisig_balance + AMOUNT
        print("OK")
