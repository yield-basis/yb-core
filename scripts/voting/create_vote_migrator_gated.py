#!/usr/bin/env python3
"""
Deploy LTMigratorGated and vote to make it a limit setter on HybridFactoryOwner, next to the fixed
LTMigrator which the pending proposal SWAP_PROPOSAL installs (it stays enabled).

Curve vote 1508 attaches YBOraclizedPolicy to the WETH market's (10) cryptopool. Its LT gate admits a
deposit only at the LevAMM book's debt / collateral ratio (to 0.01%), which the live LTMigrator's
price_oracle debt misses, so migrations into market 10 revert with "LT deviation". LTMigratorGated has
the same ABI and the same allocation handling as the fixed LTMigrator, and solves that debt on-chain, as
LTDepositZap does. It suits every other market too.

The fork run executes SWAP_PROPOSAL unless it has executed, then deploys, creates, passes and executes
this vote, attaches the policy as Curve vote 1508 would (unless it has executed), checks that the fixed
LTMigrator fails into market 10, and migrates 1% of each forward pair's gauge, plain and staked, through
the new migrator.

Usage:
  python scripts/voting/create_vote_migrator_gated.py          # deploy, verify and create the vote
  python scripts/voting/create_vote_migrator_gated.py --fork   # simulate on a fork
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

FORK = "--fork" in sys.argv[1:]

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
YB_FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
# Pending proposal (block 26204840): LTMigrator 0xd5b450... -> 0xEacb6032... (contracts/LTMigrator.vy at
# 02d793a: newest-market direction rule, no limit=1 reset of lt_from's allocation)
SWAP_PROPOSAL = 68833251590147929654715939790700980093137965976260969226758254139376820549194
LIVE_LT_MIGRATOR = "0xEacb6032d9d76D87DbD95bF05E2210820767cB0F"
SUPERSEDED_LT_MIGRATOR = "0xd5b450fDD5aF5341436Fc37A46BBEc39CA8F92CB"

# TokenVoting has a 1-day per-address createProposal cooldown
PROPOSER_KEYSTORE = "yb-deployer-2"

# --fork only
FORWARD = [(0, 7), (1, 8), (2, 9), (3, 7), (4, 8), (5, 9), (6, 10)]
GATED_MARKET = 10
CURVE_POLICY = "0xAEa95d551154443E73565bE724d59D63d72f8A7d"  # YBOraclizedPolicy, set by Curve vote 1508

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
ADMIN_ABI = '[{"name":"admin","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"}]'


def _keystore_path(name: str) -> str:
    return os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", name + ".json"))


def keystore_address(name: str) -> str:
    with open(_keystore_path(name)) as f:
        return to_checksum_address("0x" + json.load(f)["address"])


def account_load(name: str):
    with open(_keystore_path(name)) as f:
        pkey = account.Account.decrypt(json.load(f), getpass())
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


def deploy_migrator(factory, factory_owner):
    return boa.load("contracts/utils/LTMigratorGated.vy", factory.STABLECOIN(), factory_owner.address)


def build_vote_actions(factory_owner, new_migrator: str) -> list:
    return [
        Action(to=factory_owner.address, value=0,
               data=factory_owner.set_limit_setter.prepare_calldata(new_migrator, True)),
    ]


def vote_metadata(new_migrator: str) -> dict:
    return {
        "title": "Add a migrator into markets with LT-gated pools",
        "summary": (
            f"Allow LTMigratorGated {new_migrator} to set stablecoin allocations, next to "
            f"LTMigrator {LIVE_LT_MIGRATOR}, which stays enabled. Once Curve vote 1508 attaches "
            "YBOraclizedPolicy to the WETH market's pool, the policy admits deposits only at the "
            "debt / collateral ratio of the market's book, which LTMigrator's debt misses. The "
            "new migrator has the same interface and computes that debt on-chain."
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


# --- forked test -------------------------------------------------------------

def pass_vote(voting, pid: int):
    for voter in TEST_VOTERS:
        if voting.isMinParticipationReached(pid):
            break
        if voting.canVote(pid, voter, VOTE_YES):
            with boa.env.prank(voter):
                voting.vote(pid, VOTE_YES, False)
    assert voting.isMinParticipationReached(pid), f"proposal {pid} did not reach quorum"


def execute_swap_proposal(voting, factory_owner):
    """The pending LTMigrator swap, unless it has executed"""
    proposal = voting.getProposal(SWAP_PROPOSAL)
    if proposal[1]:
        print(f"  proposal {SWAP_PROPOSAL} has executed")
    else:
        pass_vote(voting, SWAP_PROPOSAL)
        end_date = proposal[2][3]
        if not voting.canExecute(SWAP_PROPOSAL) and boa.env.evm.patch.timestamp <= end_date:
            boa.env.time_travel(seconds=end_date - boa.env.evm.patch.timestamp + 1)
        assert voting.canExecute(SWAP_PROPOSAL), f"proposal {SWAP_PROPOSAL} cannot be executed"
        voting.execute(SWAP_PROPOSAL)
        print(f"  executed proposal {SWAP_PROPOSAL}")
    assert factory_owner.limit_setters(LIVE_LT_MIGRATOR)
    assert not factory_owner.limit_setters(SUPERSEDED_LT_MIGRATOR)


def _lt(factory, market_id):
    return boa.load_partial("contracts/LT.vy").at(factory.markets(market_id).lt)


def attach_curve_policy(factory):
    """What Curve vote 1508 does to the WETH market's pool, unless it has executed"""
    pool = boa.load_partial("contracts/twocrypto_pool/contracts/main/Twocrypto.vy").at(
        factory.markets(GATED_MARKET).cryptopool)
    if pool.POLICY() != CURVE_POLICY:
        with boa.env.prank(boa.loads_abi(ADMIN_ABI, name="CurveFactory").at(pool.factory()).admin()):
            pool.set_policy_contract(CURVE_POLICY)
        print(f"  attached {CURVE_POLICY} to {pool.address} as Curve vote 1508 would")
    assert pool.POLICY() == CURVE_POLICY


def _migrate(factory, migrator, i, j, staked):
    """Migrate 1% of lt_from's gauge for a fresh user; returns (preview, received)"""
    lt_from, lt_to = _lt(factory, i), _lt(factory, j)
    gauge_from = boa.loads_abi(GAUGE_ABI, name="Gauge").at(lt_from.staker())
    gauge_to = boa.loads_abi(GAUGE_ABI, name="Gauge").at(lt_to.staker())
    user = boa.env.generate_address()
    amount = lt_from.balanceOf(gauge_from.address) // 100
    with boa.env.prank(gauge_from.address):
        lt_from.transfer(user, amount)
    with boa.env.prank(user):
        if staked:
            lt_from.approve(gauge_from.address, 2**256 - 1)
            amount = gauge_from.deposit(amount, user)
            gauge_from.approve(migrator.address, 2**256 - 1)
            preview = migrator.preview_migrate_staked(lt_from.address, lt_to.address, amount)
            migrator.migrate_staked(lt_from.address, lt_to.address, amount, preview * 999 // 1000)
        else:
            lt_from.approve(migrator.address, 2**256 - 1)
            preview = migrator.preview_migrate_plain(lt_from.address, lt_to.address, amount)
            migrator.migrate_plain(lt_from.address, lt_to.address, amount, preview * 999 // 1000)
    return preview, (gauge_to if staked else lt_to).balanceOf(user)


def run_test():
    boa.fork(NETWORK, block_identifier="latest")
    factory, factory_owner, voting, ve = load_contracts()

    print("=== Pending LTMigrator swap ===")
    execute_swap_proposal(voting, factory_owner)

    print("\n=== Deploying LTMigratorGated, creating, passing and executing the vote ===")
    new_migrator = deploy_migrator(factory, factory_owner)
    print(f"  new migrator: {new_migrator.address}")
    boa.env.time_travel(seconds=86400)  # past the proposer's createProposal cooldown
    ve.checkpoint()
    with boa.env.prank(keystore_address(PROPOSER_KEYSTORE)):
        pid = voting.createProposal(*Proposal(
            metadata=b"", actions=build_vote_actions(factory_owner, new_migrator.address),
            allowFailureMap=0, startDate=0, endDate=0, voteOption=0, tryEarlyExecution=True))
    pass_vote(voting, pid)
    boa.env.time_travel(seconds=voting.minDuration() + 3600)
    assert voting.canExecute(pid)
    voting.execute(pid)
    assert factory_owner.limit_setters(new_migrator.address)
    assert factory_owner.limit_setters(LIVE_LT_MIGRATOR)
    assert not factory_owner.limit_setters(SUPERSEDED_LT_MIGRATOR)
    print(f"  proposal {pid} executed: {new_migrator.address} and {LIVE_LT_MIGRATOR} are limit setters")

    print(f"\n=== Market {GATED_MARKET} under the Curve policy ===")
    attach_curve_policy(factory)
    live_migrator = boa.load_partial("contracts/LTMigrator.vy").at(LIVE_LT_MIGRATOR)
    with boa.env.anchor():
        try:
            _migrate(factory, live_migrator, 6, GATED_MARKET, False)
            raise SystemExit("ERROR: LTMigrator passed the LT gate")
        except boa.BoaError as e:
            assert "LT deviation" in str(e), e
            print(f"  LTMigrator {LIVE_LT_MIGRATOR} 6 -> {GATED_MARKET}: reverts with LT deviation, as expected")

    print("\n=== Migrations through LTMigratorGated ===")
    for i, j in FORWARD:
        for staked in (False, True):
            with boa.env.anchor():
                preview, received = _migrate(factory, new_migrator, i, j, staked)
            assert abs(received - preview) * 10**9 <= preview
            print(f"  market {i:>2} -> {j:>2} {'staked' if staked else 'plain '}: received {received / 1e18:.6f} "
                  f"(preview {preview / 1e18:.6f})")
    print("\nOK")


def run_prod():
    boa.set_network_env(NETWORK)
    boa.env.add_account(account_load(PROPOSER_KEYSTORE), force_eoa=True)
    factory, factory_owner, voting, ve = load_contracts()
    if not factory_owner.limit_setters(LIVE_LT_MIGRATOR):
        print(f"Note: proposal {SWAP_PROPOSAL} (LTMigrator -> {LIVE_LT_MIGRATOR}) has not executed yet")

    print("=== Deploying LTMigratorGated ===")
    new_migrator = deploy_migrator(factory, factory_owner)
    print(f"  new migrator: {new_migrator.address}")
    verify_on_etherscan(new_migrator, Etherscan(api_key=ETHERSCAN_API_KEY))
    print("  verified")

    ve.checkpoint()
    metadata = pin_to_ipfs(vote_metadata(new_migrator.address)).encode()
    pid = voting.createProposal(*Proposal(
        metadata=metadata,
        actions=build_vote_actions(factory_owner, new_migrator.address),
        allowFailureMap=0, startDate=0, endDate=0, voteOption=0, tryEarlyExecution=True))
    print(f"Created proposal: {pid}")
    print("After execution: point the front-end's WETH migrations at the new migrator.")


if __name__ == "__main__":
    if FORK:
        run_test()
    else:
        run_prod()
