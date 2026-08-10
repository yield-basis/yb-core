#!/usr/bin/env python3
"""
Raise the WBTC market (ID=7) borrow/donation rate to 2 * 1.32% per year on crvUSD and its AMM fee
to 1.82%, but only after Curve ownership vote 1486 has been executed.

Curve vote 1486 (tx 0x8be1c151a6f94b5b6d5a9d2b1239dc12f22e2564e496fe6ba98d8fb5d74f9fc9) re-parametrises
the underlying crvUSD/WBTC twocrypto pool and, as its last action, installs an external fee/rebalancing
policy contract via set_policy_contract(). The new YB parameters only make sense together with that
policy, so the vote is guarded: action 1 asks the on-chain CallComparator to assert that the pool's
POLICY() is no longer the zero address. If Curve's vote has not executed yet, POLICY() is still 0x0,
the comparator reverts and the whole YB proposal reverts atomically (it stays executable, so it can
simply be re-executed once Curve's side lands).

FORK mode runs the full ordering end-to-end on a fork at head:
  1. YB actions before Curve's vote      -> must revert ("Equal", i.e. POLICY() == 0)
  2. Convex (biggest veCRV holder, ~54% of the snapshot voting power) votes yea on Curve vote 1486,
     time-travel past voteTime, executeVote                 -> POLICY() becomes non-zero
  3. YB actions again                    -> rate and AMM fee updated

    python scripts/voting/create_vote_wbtc_rate_fee_after_curve.py
"""

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
from vyper.utils import method_id

from boa.explorer import Etherscan
from boa.util.abi import abi_decode


MARKET_ID = 7                                       # yb-WBTC

RATE = int(2 * 0.0132 * 10**18) // (365 * 86400)    # 2 * 1.32% per year on crvUSD
FEE = int(0.0182 * 10**18)                          # 1.82%

FORK = False
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CALL_COMPARATOR = "0xd3BFa85dc668Aab38121bE12D69dd180301dec25"

# Curve DAO (ownership) - only used by the fork simulation to land vote 1486 before ours
CURVE_VOTING = "0xE478de485ad2fe566d49342Cbd03E49ed7DB3356"
CURVE_VOTE_ID = 1486
CURVE_VOTE_TX = "0x8be1c151a6f94b5b6d5a9d2b1239dc12f22e2564e496fe6ba98d8fb5d74f9fc9"
CONVEX_VOTER_PROXY = "0x989AEB4D175E16225E39E87D0D97A3360524AD80"

# Whale with voting power, used only to simulate createProposal in fork mode.
# In production the proposal is created by the yb-deployer account.
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])

POLICY_SELECTOR = method_id("POLICY()")
SET_POLICY_SELECTOR = method_id("set_policy_contract(address)")

CURVE_VOTING_ABI = json.dumps([
    {"name": "getVote", "stateMutability": "view", "type": "function", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "bool", "name": "open"}, {"type": "bool", "name": "executed"},
                 {"type": "uint64", "name": "startDate"}, {"type": "uint64", "name": "snapshotBlock"},
                 {"type": "uint64", "name": "supportRequired"}, {"type": "uint64", "name": "minAcceptQuorum"},
                 {"type": "uint256", "name": "yea"}, {"type": "uint256", "name": "nay"},
                 {"type": "uint256", "name": "votingPower"}, {"type": "bytes", "name": "script"}]},
    {"name": "voteTime", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "canVote", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "uint256"}, {"type": "address"}], "outputs": [{"type": "bool"}]},
    {"name": "canExecute", "stateMutability": "view", "type": "function", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "bool"}]},
    {"name": "vote", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "uint256"}, {"type": "bool"}, {"type": "bool"}], "outputs": []},
    {"name": "executeVote", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "uint256"}],
     "outputs": []},
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
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


def revert_reason(e: Exception) -> str:
    data = e.args[0] if e.args else b''
    if isinstance(data, bytes) and data[:4] == method_id("Error(string)"):
        return abi_decode("(string)", data[4:])[0]
    return str(e)


def execute_curve_vote(voting, vote_id: int):
    """Pass and execute a Curve ownership vote in the fork using Convex' veCRV."""
    vote = voting.getVote(vote_id)
    print(f"  vote {vote_id}: open={vote[0]} executed={vote[1]} yea={vote[6] / 1e18:,.0f} "
          f"nay={vote[7] / 1e18:,.0f} of {vote[8] / 1e18:,.0f} veCRV")
    if vote[1]:
        print("  already executed")
        return

    assert voting.canVote(vote_id, CONVEX_VOTER_PROXY), "Convex cannot vote"
    with boa.env.prank(CONVEX_VOTER_PROXY):
        voting.vote(vote_id, True, False)
    vote = voting.getVote(vote_id)
    print(f"  Convex voted yea: {vote[6] / 1e18:,.0f} of {vote[8] / 1e18:,.0f} veCRV "
          f"({vote[6] / vote[8]:.1%})")

    vote_end = vote[2] + voting.voteTime()
    if boa.env.evm.patch.timestamp <= vote_end:
        boa.env.time_travel(seconds=vote_end - boa.env.evm.patch.timestamp + 1)

    assert voting.canExecute(vote_id), "Curve vote cannot be executed"
    voting.executeVote(vote_id)
    print("  executed")


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load_partial('contracts/HybridFactoryOwner.vy').at(factory.admin())
    comparator = boa.load_partial('contracts/dao/CallComparator.vy').at(CALL_COMPARATOR)

    market = factory.markets(MARKET_ID)
    lt = boa.load_partial('contracts/LT.vy').at(market.lt)
    amm = boa.load_partial('contracts/AMM.vy').at(market.amm)
    pool = boa.load_partial('contracts/twocrypto_pool/contracts/main/Twocrypto.vy').at(market.cryptopool)

    symbol = lt.symbol()
    assert symbol == 'yb-WBTC', f"Market {MARKET_ID} is {symbol}, expected yb-WBTC"

    print(f"Market {MARKET_ID} ({symbol}), LT {lt.address}, AMM {amm.address}")
    print(f"  cryptopool:  {pool.address}  POLICY() = {pool.POLICY()}")
    print(f"  rate:        {amm.rate()} -> {RATE}  "
          f"({amm.rate() * 365 * 86400 / 1e18:.4%} -> {RATE * 365 * 86400 / 1e18:.4%} per year)")
    print(f"  AMM fee:     {amm.fee() / 1e18:.4%} -> {FEE / 1e18:.4%}")

    actions = [
        # Guard: revert unless the underlying Curve pool already has a policy contract installed,
        # i.e. unless Curve ownership vote 1486 has executed.
        Action(to=comparator.address, value=0,
               data=comparator.check_nonequal.prepare_calldata(pool.address, POLICY_SELECTOR, 0)),
        Action(to=factory_owner.address, value=0,
               data=factory_owner.lt_set_rate.prepare_calldata(lt.address, RATE)),
        Action(to=factory_owner.address, value=0,
               data=factory_owner.lt_set_amm_fee.prepare_calldata(lt.address, FEE)),
    ]

    title = 'WBTC market: rate to 2 x 1.32% and AMM fee to 1.82% (after Curve vote 1486)'
    summary = ('Set the yb-WBTC (market 7) borrow/donation rate to 2 * 1.32% per year on crvUSD and its AMM fee to '
               '1.82%. Both changes belong together with the new crvUSD/WBTC pool parameters and fee policy installed '
               'by Curve ownership vote 1486, so this proposal is guarded: it first asserts that the pool already has '
               'a policy contract set (POLICY() != 0x0) and reverts otherwise. It therefore cannot take effect before '
               'the Curve vote is executed, and can simply be executed once that has happened.')

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
        curve_voting = boa.loads_abi(CURVE_VOTING_ABI, name="CurveVoting").at(CURVE_VOTING)
        curve_script = curve_voting.getVote(CURVE_VOTE_ID)[9]
        assert SET_POLICY_SELECTOR in curve_script and bytes.fromhex(pool.address[2:]) in curve_script, \
            f"Curve vote {CURVE_VOTE_ID} does not set a policy on {pool.address}"
        print(f"\nCurve vote {CURVE_VOTE_ID} ({CURVE_VOTE_TX}) sets a policy contract on the pool")

        print("\n=== YB vote before Curve vote 1486: must revert ===")
        assert pool.POLICY() == ZERO_ADDRESS, "Pool already has a policy - guard is a no-op"
        with boa.env.anchor():
            try:
                with boa.env.prank(DAO):
                    for action in actions:
                        boa.env.raw_call(to_address=action.to, data=action.data)
                raise SystemExit("ERROR: YB vote executed while POLICY() is still 0x0!")
            except Exception as e:
                print(f"  correctly reverted: {revert_reason(e)}")

        print(f"\n=== Executing Curve ownership vote {CURVE_VOTE_ID} via Convex ===")
        execute_curve_vote(curve_voting, CURVE_VOTE_ID)
        print(f"  pool POLICY() = {pool.POLICY()}")
        assert pool.POLICY() != ZERO_ADDRESS, "Curve vote did not install a policy"

        print("\n=== YB vote after Curve vote 1486 ===")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                boa.env.raw_call(to_address=action.to, data=action.data)
                print(f"  action {i + 1} out of {len(actions)}")

        print(f"  rate:    {amm.rate()} ({amm.rate() * 365 * 86400 / 1e18:.4%} per year)")
        print(f"  AMM fee: {amm.fee() / 1e18:.4%}")
        assert amm.rate() == RATE, "Rate not updated as expected"
        assert amm.fee() == FEE, "Fee not updated as expected"
        print("OK")
