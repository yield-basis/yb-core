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


FORK = False

RATE = 1 / (4 * 365 * 86400)


VESTING_SHIFT = 5 * 60  # s
VEST_TYPES = {
        0: 'inflation itself',
        1: '2 year vest with 6 months cliff',
        2: 'no vest and no cliff',
        3: 'inflation-like vest'
}

vests = defaultdict(list)


VotingSettings = namedtuple('VotingSettings', ['votingMode', 'supportThreshold', 'minParticipation', 'minDuration',
                                               'minProposerVotingPower'])
VotingExtendedParams = namedtuple('VotingExtendedParams', ['minApprovals', 'excludedAccounts', 'decayMidpoint',
                                                           'cooldown'])
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


DAO_SUBDOMAIN = "ybdaot1"  # XXX change
DAO_URI = ""  # ?
VOTE_SETTINGS = VotingSettings(
    votingMode=1,                   # 0 = no early execution, 1 = enable it. Switch 1->0 after 1st markets are seeded
    supportThreshold=int(0.55e6),   # 1e6 base
    minParticipation=int(0.3e6),    # 1e6 base
    minDuration=7 * 86400,          # s
    minProposerVotingPower=1        # with NFTs 1 = has position, 0 = has no position
)
EXTENDED_PARAMS = VotingExtendedParams(
    minApprovals=1,
    excludedAccounts=[],
    decayMidpoint=5000,
    cooldown=86400
)
TARGET_CONFIG = (ZERO_ADDRESS, 0)
DAO_DESCRIPTION = {
    'name': 'Yield Basis DAO',
    'description': '',
    'links': []
}
PLUGIN_DESCRIPTION = {
    'name': 'Yield Basis Proposal',
    'description': '',
    'links': [],
    'processKey': 'YBP'
}

TOKEN_VOTING_FACTORY = "0x1293C8E86b1d7055C07C32B0c37Ed8C14F0C5D10"
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer

USD_TOKEN = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"  # crvUSD
AGG = "0x18672b1b0c623a30089A280Ed9256379fb0E4E62"  # crvUSD aggregator
FLASH = "0x26dE7861e213A5351F6ED767d00e0839930e9eE1"
FEE_RECEIVER = "0x0000000000000000000000000000000000000000"  # XXX
EMERGENCY_ADMIN = "0x467947EE34aF926cF1DCac093870f613C96B1E0c"

ETHERSCAN_URL = "https://api.etherscan.io/api"

EXTRA_TIMEOUT = 30


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
        sleep(EXTRA_TIMEOUT)
        verify(yb, etherscan, wait=False)
    ve_yb = boa.load('contracts/dao/VotingEscrow.vy', yb.address, 'VotingEscrow: Yield Basis', 'veYB', '')
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(ve_yb, etherscan, wait=False)
    vpc = boa.load('contracts/dao/VotingPowerCondition.vy', ve_yb.address, 2_500 * 10**18)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(vpc, etherscan, wait=False)
    gc = boa.load('contracts/dao/GaugeController.vy', yb.address, ve_yb.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(gc, etherscan, wait=False)

    # Vests with cliff (1)

    cliff_impl = boa.load('contracts/dao/CliffEscrow.vy', yb.address, ve_yb.address, gc.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(cliff_impl, etherscan, wait=False)
    t0 = int(time()) + VESTING_SHIFT
    t1 = t0 + 2 * 365 * 86400
    vesting = boa.load('contracts/dao/VestingEscrow.vy', yb.address, t0, t1, True, cliff_impl.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(vesting, etherscan, wait=False)

    recipients = [row[0] for row in vests[1]]
    amounts = [int(row[1] * 10**18) for row in vests[1]]
    total = sum(amounts)

    yb.approve(vesting.address, 2**256 - 1)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
    yb.mint(admin, total)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
    vesting.add_tokens(total)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
    vesting.fund(recipients, amounts, t0 + 365 * 86400 // 2)
    if not FORK:
        sleep(EXTRA_TIMEOUT)

    # No vesting no cliff allocations (2)

    for address, amount, comment in vests[2]:
        yb.mint(address, int(amount * 10**18))
        if not FORK:
            sleep(EXTRA_TIMEOUT)

    # Inflation-like vest(s) (3)

    for address, amount, comment in vests[3]:
        ivest = boa.load('contracts/dao/InflationaryVest.vy', yb.address, address, admin)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        yb.mint(ivest.address, int(amount * 10**18))
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        ivest.start()
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        print(f"IVest:   {ivest.address} for {address}")

    # 1 year delay + 1 year vest (4)

    t0 = int(time()) + 365 * 86400
    t1 = t0 + 365 * 86400
    vesting_1y = boa.load('contracts/dao/VestingEscrow.vy', yb.address, t0, t1, True, cliff_impl.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(vesting_1y, etherscan, wait=False)

    if vests[4]:
        recipients = [row[0] for row in vests[4]]
        amounts = [int(row[1] * 10**18) for row in vests[4]]
        total = sum(amounts)

        yb.approve(vesting_1y.address, 2**256 - 1)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        yb.mint(admin, total)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        vesting_1y.add_tokens(total)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        vesting_1y.fund(recipients, amounts, 0)
        if not FORK:
            sleep(EXTRA_TIMEOUT)

    # 1 month delay + 2 year vest (5)

    t0 = int(time()) + 30 * 86400
    t1 = t0 + 2 * 365 * 86400
    vesting_2y = boa.load('contracts/dao/VestingEscrow.vy', yb.address, t0, t1, True, cliff_impl.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(vesting_2y, etherscan, wait=False)

    if vests[5]:
        recipients = [row[0] for row in vests[5]]
        amounts = [int(row[1] * 10**18) for row in vests[5]]
        total = sum(amounts)

        yb.approve(vesting_2y.address, 2**256 - 1)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        yb.mint(admin, total)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        vesting_2y.add_tokens(total)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        vesting_2y.fund(recipients, amounts, 0)
        if not FORK:
            sleep(EXTRA_TIMEOUT)

    # Immediate 1 year vest (6)

    t0 = int(time()) + 30 * 86400
    t1 = t0 + 2 * 365 * 86400
    vesting_1yi = boa.load('contracts/dao/VestingEscrow.vy', yb.address, t0, t1, True, cliff_impl.address)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(vesting_1yi, etherscan, wait=False)

    if vests[6]:
        recipients = [row[0] for row in vests[6]]
        amounts = [int(row[1] * 10**18) for row in vests[6]]
        total = sum(amounts)

        yb.approve(vesting_1yi.address, 2**256 - 1)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        yb.mint(admin, total)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        vesting_1yi.add_tokens(total)
        if not FORK:
            sleep(EXTRA_TIMEOUT)
        vesting_1yi.fund(recipients, amounts, 0)
        if not FORK:
            sleep(EXTRA_TIMEOUT)

    # Aragon

    factory = boa.load_abi(os.path.dirname(__file__) + '/TokenVotingFactory.abi.json', name="TVFactory").at(TOKEN_VOTING_FACTORY)
    deployed_dao = factory.deployDAOWithTokenVoting((
        DAO_SUBDOMAIN,
        pin_to_ipfs(DAO_DESCRIPTION).encode(),
        DAO_URI,
        ve_yb.address,
        VOTE_SETTINGS,
        TARGET_CONFIG,
        pin_to_ipfs(PLUGIN_DESCRIPTION).encode(),
        EXTENDED_PARAMS
    ))
    if not FORK:
        sleep(EXTRA_TIMEOUT)

    # Deploy AMM, LT, vpool, lp oracle, gauge impl, stake zap, factory with dao as an admin
    amm_interface = boa.load_partial('contracts/AMM.vy')
    yb_amm_impl = amm_interface.deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        yb_amm_impl.ctor_calldata = b""
        verify(yb_amm_impl, etherscan, wait=False)
    lt_interface = boa.load_partial('contracts/LT.vy')
    yb_lt_impl = lt_interface.deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        yb_lt_impl.ctor_calldata = b""
        verify(yb_lt_impl, etherscan, wait=False)
    vpool_impl = boa.load_partial('contracts/VirtualPool.vy').deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        vpool_impl.ctor_calldata = b""
        verify(vpool_impl, etherscan, wait=False)
    oracle_impl = boa.load_partial('contracts/CryptopoolLPOracle.vy').deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        oracle_impl.ctor_calldata = b""
        verify(oracle_impl, etherscan, wait=False)
    gauge_impl = boa.load_partial('contracts/dao/LiquidityGauge.vy').deploy_as_blueprint()
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        gauge_impl.ctor_calldata = b""
        verify(gauge_impl, etherscan, wait=False)
    stake_zap = boa.load('contracts/dao/StakeZap.vy')
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(stake_zap, etherscan, wait=False)

    yb_factory = boa.load(
        'contracts/Factory.vy',
        USD_TOKEN,
        yb_amm_impl,
        yb_lt_impl,
        vpool_impl,
        oracle_impl,
        gauge_impl,
        AGG,
        FLASH,
        FEE_RECEIVER,
        gc.address,
        deployed_dao.dao,
        EMERGENCY_ADMIN)
    if not FORK:
        sleep(EXTRA_TIMEOUT)
        verify(yb_factory, etherscan, wait=False)

    # Transfer to Aragon:
    # gc
    gc.transfer_ownership(deployed_dao.dao)

    # Transfer to YB co
    vesting.transfer_ownership("0xC1671c9efc9A2ecC347238BeA054Fc6d1c6c28F9")
    vesting_1y.transfer_ownership("0xC1671c9efc9A2ecC347238BeA054Fc6d1c6c28F9")  # XXX
    vesting_2y.transfer_ownership("0xC1671c9efc9A2ecC347238BeA054Fc6d1c6c28F9")  # XXX
    vesting_1yi.transfer_ownership("0xC1671c9efc9A2ecC347238BeA054Fc6d1c6c28F9")  # XXX

    # YB set minter to GC

    # YB STILL has deployer as an admin, it needs to start emissions and renounce ownership later

    print(f"YB:         {yb.address}")
    print(f"veYB:       {ve_yb.address}")
    print(f"GC:         {gc.address}")
    print(f"CE:         {cliff_impl.address}")
    print(f"Vest:       {vesting.address}")
    print(f"Vest 1y:    {vesting_1y.address}")
    print(f"Vest 2y:    {vesting_2y.address}")
    print(f"Vest 1yi:   {vesting_1yi.address}")
    print()
    print(f"DAO:    {deployed_dao.dao}")
    print(f"Plugin: {deployed_dao.plugin}")
    print(f"Cond:   {deployed_dao.condition}")
    print()
    print(f"Factory: {yb_factory.address}")
    print(f"StakeZap: {stake_zap.address}")
