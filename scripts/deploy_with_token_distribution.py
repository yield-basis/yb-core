#!/usr/bin/env python3

# This is to test Aragon deployments mostly

import boa
import json
import requests
import os
import csv
from time import time
from time import sleep
from boa.explorer import Etherscan
from boa.verifiers import verify
from collections import namedtuple
from eth_account import account
from getpass import getpass
from collections import defaultdict

from networks import NETWORK
from networks import ETHERSCAN_API_KEY
from networks import PINATA_TOKEN


FORK = True

RATE = 1 / (4 * 365 * 86400)


VEST_TYPES = {
        0: 'inflation itself',
        1: '2 year vest with 6 months cliff',
        2: 'no vest and no cliff',
        3: 'inflation-like vest'
}

vests = defaultdict(list)


VotingSettings = namedtuple('VotingSettings', ['votingMode', 'supportThreshold', 'minParticipation', 'minDuration',
                                               'minProposerVotingPower'])
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


DAO_SUBDOMAIN = ""  # ?
DAO_URI = ""  # ?
VOTE_SETTINGS = VotingSettings(
    votingMode=1,                   # 0 = no early execution, 1 = enable it. Switch 1->0 after 1st markets are seeded
    supportThreshold=int(0.55e6),   # 1e6 base
    minParticipation=int(0.3e6),    # 1e6 base
    minDuration=7 * 86400,          # s
    minProposerVotingPower=1        # with NFTs 1 = has position, 0 = has no position
)
TARGET_CONFIG = (ZERO_ADDRESS, 0)  # ??
MIN_APPROVALS = 1  # ?
DAO_DESCRIPTION = {
    'name': 'Test YB DAO',
    'description': '',
    'links': []
}
PLUGIN_DESCRIPTION = {
    'name': 'Yield Basis Proposal',
    'description': 'Temporary voting plugin before the one with decay is applied',
    'links': [],
    'processKey': 'YBP'
}

TOKEN_VOTING_FACTORY = "0x076E298405b75a02C222D3860d703E4C92FB9E20"
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer


TV_FACTORY_ABI = """
[
{"inputs":[{"components":[{"internalType":"string","name":"daoSubdomain","type":"string"},{"internalType":"bytes","name":"daoMetadata","type":"bytes"},{"internalType":"string","name":"daoURI","type":"string"},{"internalType":"address","name":"token","type":"address"},{"components":[{"internalType":"enum MajorityVotingBase.VotingMode","name":"votingMode","type":"uint8"},{"internalType":"uint32","name":"supportThreshold","type":"uint32"},{"internalType":"uint32","name":"minParticipation","type":"uint32"},{"internalType":"uint64","name":"minDuration","type":"uint64"},{"internalType":"uint256","name":"minProposerVotingPower","type":"uint256"}],"internalType":"struct MajorityVotingBase.VotingSettings","name":"votingSettings","type":"tuple"},{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"enum IPlugin.Operation","name":"operation","type":"uint8"}],"internalType":"struct IPlugin.TargetConfig","name":"targetConfig","type":"tuple"},{"internalType":"uint256","name":"minApprovals","type":"uint256"},{"internalType":"bytes","name":"pluginMetadata","type":"bytes"}],"internalType":"struct TokenVotingFactory.DeploymentSettings","name":"settings","type":"tuple"}],"name":"deployDAOWithTokenVoting","outputs":[{"components":[{"internalType":"contract DAO","name":"dao","type":"address"},{"internalType":"contract TokenVoting","name":"plugin","type":"address"},{"internalType":"address","name":"token","type":"address"},{"internalType":"contract VotingPowerCondition","name":"condition","type":"address"}],"internalType":"struct TokenVotingFactory.Deployment","name":"deployment","type":"tuple"}],"stateMutability":"nonpayable","type":"function"},
{"inputs":[{"internalType":"uint256","name":"_proposalId","type":"uint256"},{"internalType":"enum IMajorityVoting.VoteOption","name":"_voteOption","type":"uint8"},{"internalType":"bool","name":"_tryEarlyExecution","type":"bool"}],"name":"vote","outputs":[],"stateMutability":"nonpayable","type":"function"}
]
"""

ETHERSCAN_URL = "https://api.etherscan.io/api"


def read_data():
    name = os.path.dirname(__file__) + '/token_distribution_example.csv'
    with open(name, 'r') as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) > 0:
                if row[0] in ['0', '1', '2', '3']:
                    yield [int(row[0]), row[1].strip(), float(row[2]), ','.join(row[3:])]


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


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


if __name__ == '__main__':
    for data_type, addr, amount, comment in read_data():
        vests[data_type].append((addr, amount, comment))

    if FORK:
        boa.fork(NETWORK)
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(ETHERSCAN_URL, ETHERSCAN_API_KEY)

    if FORK:
        admin = DEPLOYER
        boa.env.eoa = admin
    else:
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)

    yb = boa.load('contracts/dao/YB.vy', int(vests[0][0][1] * 10**18), int(RATE * vests[0][0][1] * 10**18))
    if not FORK:
        sleep(30)
        verify(yb, etherscan, wait=False)
    ve_yb = boa.load('contracts/dao/VotingEscrow.vy', yb.address, 'Yield Basis', 'YB', '')
    if not FORK:
        sleep(30)
        verify(ve_yb, etherscan, wait=False)
    vpc = boa.load('contracts/dao/VotingPowerCondition.vy', ve_yb.address, 2_500 * 10**18)
    if not FORK:
        sleep(30)
        verify(vpc, etherscan, wait=False)
    gc = boa.load('contracts/dao/GaugeController.vy', yb.address, ve_yb.address)
    if not FORK:
        sleep(30)
        verify(gc, etherscan, wait=False)

    # Vests with cliff (1)

    cliff_impl = boa.load('contracts/dao/CliffEscrow.vy', yb.address, ve_yb.address, gc.address)
    if not FORK:
        sleep(30)
        verify(cliff_impl, etherscan, wait=False)
    t0 = int(time())
    t1 = t0 + 2 * 365 * 86400
    vesting = boa.load('contracts/dao/VestingEscrow.vy', yb.address, t0, t1, True, cliff_impl.address)
    if not FORK:
        sleep(30)
        verify(vesting, etherscan, wait=False)

    recipients = [row[0] for row in vests[1]]
    amounts = [int(row[1] * 10**18) for row in vests[1]]
    total = sum(amounts)

    yb.approve(vesting.address, 2**256 - 1)
    if not FORK:
        sleep(30)
    yb.mint(admin, total)
    if not FORK:
        sleep(30)
    vesting.add_tokens(total)
    if not FORK:
        sleep(30)
    vesting.fund(recipients, amounts, t0 + 365 * 86400 // 2)
    if not FORK:
        sleep(30)

    # No vesting no cliff allocations (2)

    for address, amount, comment in vests[2]:
        yb.mint(address, int(amount * 10**18))
        if not FORK:
            sleep(30)

    print(f"YB:      {yb.address}")
    print(f"veYB:    {ve_yb.address}")
    print(f"GC:      {gc.address}")
    print(f"CE:      {cliff_impl.address}")

    # Inflation-like vest(s) (3)
    for address, amount, comment in vests[3]:
        ivest = boa.load('contracts/dao/InflationaryVest.vy', yb.address, address, admin)
        if not FORK:
            sleep(30)
        yb.mint(ivest.address, int(amount * 10**18))
        if not FORK:
            sleep(30)
        ivest.start()
        if not FORK:
            sleep(30)
        print(f"IVest:   {ivest.address} for {address}")

    # Aragon

    factory = boa.loads_abi(TV_FACTORY_ABI, name="TVFactory").at(TOKEN_VOTING_FACTORY)
    deployed_dao = factory.deployDAOWithTokenVoting((
        DAO_SUBDOMAIN,
        pin_to_ipfs(DAO_DESCRIPTION).encode(),
        DAO_URI,
        ve_yb.address,
        VOTE_SETTINGS,
        TARGET_CONFIG,
        MIN_APPROVALS,
        pin_to_ipfs(PLUGIN_DESCRIPTION).encode()
    ))
    if not FORK:
        sleep(30)
    # has dao, plugin, token and condition addributes
