#!/usr/bin/env python3
"""
Deploy a new LTMigrator and vote to replace the live one.

Usage:
  python scripts/voting/create_vote_migrator_direction_fix.py          # prod
  python scripts/voting/create_vote_migrator_direction_fix.py --test   # fork
"""
import json
import os
import sys
from collections import namedtuple
from getpass import getpass
from time import sleep

import boa
import requests
from eth_account import account
from eth_utils import to_checksum_address

from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify

from networks import NETWORK, PINATA_TOKEN, ETHERSCAN_API_KEY

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
YB_FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
LIVE_LT_MIGRATOR = "0xd5b450fDD5aF5341436Fc37A46BBEc39CA8F92CB"  # from e894412

# TokenVoting has a 1-day per-address createProposal cooldown
PROPOSER_KEYSTORE = "yb-deployer"

# Current allocations, crvUSD (block 26200431)
ALLOCATIONS = {
    7: 99_999_113,      # yb-WBTC
    8: 100_000_303,     # yb-cbBTC
    9: 35_740_000,      # yb-tBTC
    10: 62_123_655,     # yb-WETH
}

FORWARD = [(0, 7), (3, 7), (6, 10)]  # --test only

# Largest veYB voters -- forked dry-run only (scripts/voting/find_ve_voters.py)
TEST_VOTERS = [
    "0xD10782f4D4fE20f5E15f3347E5D59aB3eC6D63d6",
    "0xb75BB762e5D62ca8f23FAbC87b1f651d0171d521",
    "0x0070D9adC687a28FBAcC0a0Aab24B90c037AD24e",
    "0x0000000C90799449af8eE0B240Da639144a36C6A",
    "0x29B6a3512FafeAce91433D278503ABC3D5aB5d12",
    "0x64A0f7Ca6efbFDaF975B5127650EC9f175182d7b",
    "0x2DE99955fA0318556f3542E71421EFD1BCF3293a",
    "0xc64566f526F09eEeFbd284760D0E1b94c6e69fc8",
    "0xb373e9C6D3Ad91eD03AA4b3C8106e68Bf3de192f",
    "0xC2059e0F6C4c1D441BAf295BC6Ea15cE24c71701",
    "0x2029Df02833eb9cd83A5143AbfE5d65BC4B46A16",
    "0xEeee8f503bCF020bc0909D35FBAbEe9ffD24641a",
    "0xaE922Cea2323fD75a31e28d10E0c84F49e1Fb6b7",
    "0xc900C3EFDC7d790f50b0B43b0c4206F8141a77eC",
    "0x52658B966801D8ddB6f49c886557084EC459Aa27",
    "0x53F8Ef5B139824C85d3f837C6b535470bFBFEeE3",
    "0xfF642740228d6abe9786612A421C0B1b859f0793",
    "0x0ca0e5c9f426d1e3EF1f568862716c68b1f44a01",
    "0x9cB95F0Fa3dB70eb15D1505d3c73a55766A1Ec89",
    "0x387ACB7f56A6f29137a21d7Eb755A3F638cab45B",
    "0x3340a1541296E7A4db2Ac6f9C3776bda7eF62724",
    "0xF791da446D04282f921f38FBF954aD5cAee899a3",
    "0x27c6DCcF18d6e66942dc5a250B1618B6B3A44036",
    "0x858a2FEf6C0833f8a5532122bb964655b0E6d5B5",
    "0x1105c075A4034C57d1457151b2D7cAb2ed99B122",
]

VOTE_YES = 2
VERIFY_RETRY_SECONDS = 10

Proposal = namedtuple(
    "Proposal",
    ["metadata", "actions", "allowFailureMap", "startDate", "endDate",
     "voteOption", "tryEarlyExecution"],
)
Action = namedtuple("Action", ["to", "value", "data"])
ABI_DIR = os.path.dirname(os.path.abspath(__file__))

GAUGE_ABI = """[
 {"name":"deposit","outputs":[{"type":"uint256"}],"inputs":[{"type":"uint256","name":"assets"},{"type":"address","name":"receiver"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"}
]"""


def _keystore_path(name: str) -> str:
    return os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", name + ".json"))


def keystore_address(name: str) -> str:
    with open(_keystore_path(name)) as f:
        return to_checksum_address("0x" + json.load(f)["address"])


def account_load(name: str):
    with open(_keystore_path(name)) as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
    return account.Account.from_key(pkey)


def load_contracts():
    factory = boa.load_partial("contracts/Factory.vy").at(YB_FACTORY)
    factory_owner = boa.load_partial("contracts/HybridFactoryOwner.vy").at(
        factory.admin())
    voting = boa.load_abi(
        os.path.join(ABI_DIR, "TokenVoting.abi.json"), name="AragonVoting"
    ).at(VOTING_PLUGIN)
    ve = boa.load_partial("contracts/dao/VotingEscrow.vy").at(
        voting.getVotingToken())
    return factory, factory_owner, voting, ve


def build_vote_actions(factory, factory_owner, new_migrator: str) -> list:
    actions = [
        Action(to=factory_owner.address, value=0,
               data=factory_owner.set_limit_setter.prepare_calldata(new_migrator, True)),
        Action(to=factory_owner.address, value=0,
               data=factory_owner.set_limit_setter.prepare_calldata(LIVE_LT_MIGRATOR, False)),
    ]
    for market_id, allocation in sorted(ALLOCATIONS.items()):
        actions.append(Action(
            to=factory_owner.address, value=0,
            data=factory_owner.lt_allocate_stablecoins.prepare_calldata(
                factory.markets(market_id).lt, allocation * 10**18)))
    return actions


def vote_metadata(new_migrator: str) -> dict:
    return {
        "title": "Replace the LTMigrator",
        "summary": (
            f"Replace the LTMigrator {LIVE_LT_MIGRATOR} with {new_migrator}, which also "
            "migrates from v1 markets, and introduces a few fixes. Re-set the allocations of markets 7-10."
        ),
        "resources": [],
    }


def pin_to_ipfs(content: dict) -> str:
    response = requests.post(
        "https://api.pinata.cloud/pinning/pinJSONToIPFS",
        json={"pinataContent": content,
              "pinataMetadata": {"name": "pinnie.json"},
              "pinataOptions": {"cidVersion": 1}},
        headers={"Authorization": f"Bearer {PINATA_TOKEN}",
                 "Content-Type": "application/json"},
        timeout=30,
    )
    assert 200 <= response.status_code < 400, response.text
    return "ipfs://" + response.json()["IpfsHash"]


def verify_on_etherscan(contract, etherscan):
    while True:
        try:
            sleep(VERIFY_RETRY_SECONDS)
            boa_verify(contract, etherscan, wait=True)
            return
        except ValueError as e:
            print(e)
            if "Already Verified" in str(e):
                return


def print_allocations(factory):
    for market_id, target in sorted(ALLOCATIONS.items()):
        lt = boa.load_partial("contracts/LT.vy").at(factory.markets(market_id).lt)
        print(f"  market {market_id:>2}: allocation {lt.stablecoin_allocation() / 1e18:>14,.0f}"
              f"  vote target {target:>14,}")


# --- forked test -------------------------------------------------------------

def pass_vote(voting, pid: int):
    for voter in TEST_VOTERS:
        if voting.isMinParticipationReached(pid):
            break
        if voting.canVote(pid, voter, VOTE_YES):
            with boa.env.prank(voter):
                voting.vote(pid, VOTE_YES, False)
    assert voting.isMinParticipationReached(pid), f"proposal {pid} did not reach quorum"


def _lt(factory, market_id):
    return boa.load_partial("contracts/LT.vy").at(factory.markets(market_id).lt)


def _fund(lt, to, amount):
    """Source LT shares from the market's gauge"""
    with boa.env.prank(lt.staker()):
        lt.transfer(to, amount)


def _migrate(factory, migrator, i, j, staked):
    lt_from, lt_to = _lt(factory, i), _lt(factory, j)
    gauge_from = boa.loads_abi(GAUGE_ABI).at(lt_from.staker())
    gauge_to = boa.loads_abi(GAUGE_ABI).at(lt_to.staker())
    user = boa.env.generate_address()
    amount = lt_from.balanceOf(gauge_from.address) // 100
    _fund(lt_from, user, amount)
    with boa.env.prank(user):
        if staked:
            lt_from.approve(gauge_from.address, 2**256 - 1)
            amount = gauge_from.deposit(amount, user)
            gauge_from.approve(migrator.address, 2**256 - 1)
            migrator.migrate_staked(lt_from.address, lt_to.address, amount, 0)
        else:
            lt_from.approve(migrator.address, 2**256 - 1)
            migrator.migrate_plain(lt_from.address, lt_to.address, amount, 0)
    received = (gauge_to if staked else lt_to).balanceOf(user)
    assert received > 0
    return received


def run_test():
    boa.fork(NETWORK, block_identifier="latest")
    factory, factory_owner, voting, ve = load_contracts()
    assert factory_owner.limit_setters(LIVE_LT_MIGRATOR)
    print("=== Current allocations ===")
    print_allocations(factory)

    print("\n=== Deploying the new LTMigrator, creating, passing and executing the vote ===")
    new_migrator = boa.load("contracts/LTMigrator.vy", factory.STABLECOIN(), factory_owner.address)
    ve.checkpoint()
    with boa.env.prank(keystore_address(PROPOSER_KEYSTORE)):
        pid = voting.createProposal(*Proposal(
            metadata=b"", actions=build_vote_actions(factory, factory_owner, new_migrator.address),
            allowFailureMap=0, startDate=0, endDate=0, voteOption=0, tryEarlyExecution=True))
    pass_vote(voting, pid)
    boa.env.time_travel(seconds=voting.minDuration() + 3600)
    assert voting.canExecute(pid)
    voting.execute(pid)
    assert factory_owner.limit_setters(new_migrator.address)
    assert not factory_owner.limit_setters(LIVE_LT_MIGRATOR)
    print(f"  proposal {pid} executed; new migrator {new_migrator.address}")

    print("\n=== Allocations after execution ===")
    print_allocations(factory)
    for market_id, target in ALLOCATIONS.items():
        assert _lt(factory, market_id).stablecoin_allocation() >= target * 10**18

    print("\n=== Migrations through the new migrator ===")
    for i, j in FORWARD:
        for staked in (False, True):
            with boa.env.anchor():
                received = _migrate(factory, new_migrator, i, j, staked)
            print(f"  market {i:>2} -> {j:>2} {'staked' if staked else 'plain '}: received {received / 1e18:.6f}")
    print("\nOK")


def run_prod():
    boa.set_network_env(NETWORK)
    boa.env.add_account(account_load(PROPOSER_KEYSTORE), force_eoa=True)
    factory, factory_owner, voting, ve = load_contracts()
    print("=== Current allocations ===")
    print_allocations(factory)

    print("\n=== Deploying the new LTMigrator ===")
    new_migrator = boa.load("contracts/LTMigrator.vy", factory.STABLECOIN(), factory_owner.address)
    print(f"  new migrator: {new_migrator.address}")
    verify_on_etherscan(new_migrator, Etherscan(api_key=ETHERSCAN_API_KEY))
    print("  verified")

    ve.checkpoint()
    metadata = pin_to_ipfs(vote_metadata(new_migrator.address)).encode()
    pid = voting.createProposal(*Proposal(
        metadata=metadata,
        actions=build_vote_actions(factory, factory_owner, new_migrator.address),
        allowFailureMap=0, startDate=0, endDate=0, voteOption=0, tryEarlyExecution=True))
    print(f"Created proposal: {pid}")
    print("After execution: point the front-end at the new migrator address.")


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        run_test()
    else:
        run_prod()
