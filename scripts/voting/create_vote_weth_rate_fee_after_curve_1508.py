#!/usr/bin/env python3
"""
Set the WETH (ID=10) market's donation APY to 2.60% per year on crvUSD (2x leverage -> 1.30% pool donation
APY) and its releverage fee to 150 bps, but only after Curve ownership vote 1508 has been executed.

Curve vote 1508 attaches YBOraclizedPolicy (Pyth-report-driven fees and price_scale) to the crvUSD/WETH
twocrypto pool and sets its reserved profit fraction to 43%: set_fee_parameters(43%, 0) followed by
set_policy_contract(). The research behind it (forum note "Oracle reports for YieldBasis pools", 2026-10-21)
tunes the donation rate and the LevAMM releverage fee together with that policy and profit fraction, so the
new YB parameters only make sense once the policy is live. The vote is therefore guarded: action 1 asks the
on-chain CallComparator to assert that the pool's POLICY() is already the contract installed by vote 1508.
If Curve's vote has not executed yet, POLICY() is still empty, the comparator reverts and the whole YB
proposal reverts atomically (it stays executable, so it can simply be re-executed once Curve's side lands).

Fork mode runs the full ordering end-to-end on a fork at head:
  1. YB actions before Curve's vote      -> must revert ("Not equal", i.e. no policy yet)
  2. Convex (biggest veCRV holder) votes yea on Curve vote 1508, time-travel past voteTime,
     executeVote                          -> POLICY() becomes YBOraclizedPolicy, RPF 43%
  3. YB actions again                    -> rate and AMM fee updated
  4. LT keeps working under the policy: borrower fees are donated, and a deposit (with debt in the
     LevAMM's book ratio, as the policy's LT gate requires) and a withdrawal go through

    python scripts/voting/create_vote_weth_rate_fee_after_curve_1508.py           # create the vote
    python scripts/voting/create_vote_weth_rate_fee_after_curve_1508.py --fork    # simulate on a fork
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
from vyper.utils import method_id

from boa.util.abi import abi_decode


MARKET_ID = 10                                        # yb-WETH
MARKET_SYMBOL = 'yb-WETH'

# Policy contract which Curve ownership vote 1508 installs on the market's cryptopool. The YB vote is gated on
# it being live, so it doubles as the guard value.
POOL = '0x656341Ef90b622c6634e0573772FfB7f3669b9f3'   # crvUSD/WETH
NEW_POLICY = '0xAEa95d551154443E73565bE724d59D63d72f8A7d'  # YBOraclizedPolicy
NEW_RPF = 43 * 10**10 // 100                          # reserved_profit_fraction set by vote 1508 (10**10 precision)

RATE = int(2 * 0.0130 * 10**18) // (365 * 86400)      # 2 * 1.30% pool donation APY = 2.60% per year
FEE = int(0.0150 * 10**18)                            # 150 bps releverage fee

FORK = "--fork" in sys.argv[1:]

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CALL_COMPARATOR = "0xd3BFa85dc668Aab38121bE12D69dd180301dec25"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

# Curve DAO (ownership) - only used by the fork simulation to land vote 1508 before ours
CURVE_VOTING = "0xE478de485ad2fe566d49342Cbd03E49ed7DB3356"
CURVE_VOTE_ID = 1508
CONVEX_VOTER_PROXY = "0x989AEB4D175E16225E39E87D0D97A3360524AD80"

# Whale with voting power, used only to simulate createProposal in fork mode.
# In production the proposal is created by the yb-deployer account.
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"
DEPOSITOR = "0x1111111111111111111111111111111111111111"  # fork mode only

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])

POLICY_SELECTOR = method_id("POLICY()")
SET_POLICY_SELECTOR = method_id("set_policy_contract(address)")
SET_FEE_PARAMETERS_SELECTOR = method_id("set_fee_parameters(uint256,uint256)")

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

WETH_ABI = json.dumps([
    {"name": "deposit", "stateMutability": "payable", "type": "function", "inputs": [], "outputs": []},
    {"name": "approve", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "address", "name": "spender"}, {"type": "uint256", "name": "amount"}],
     "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "address", "name": "owner"}], "outputs": [{"type": "uint256"}]},
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


def proportional_debt(amm, pool, assets: int) -> int:
    """
    Debt for an LT deposit of `assets` which keeps the LevAMM's debt / collateral ratio, as the policy's LT gate
    requires (to 0.01%): debt == minted LP * amm debt / amm collateral, solved by fixed-point iteration.
    """
    debt = assets * pool.price_scale() // 10**18
    for _ in range(100):
        new_debt = pool.calc_token_amount([debt, assets], True) * amm.get_debt() // amm.collateral_amount()
        if new_debt == debt:
            break
        debt = new_debt
    return debt


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load_partial('contracts/HybridFactoryOwner.vy').at(factory.admin())
    comparator = boa.load_partial('contracts/dao/CallComparator.vy').at(CALL_COMPARATOR)

    market = factory.markets(MARKET_ID)
    lt = boa.load_partial('contracts/LT.vy').at(market.lt)
    amm = boa.load_partial('contracts/AMM.vy').at(market.amm)
    pool = boa.load_partial('contracts/twocrypto_pool/contracts/main/Twocrypto.vy').at(market.cryptopool)

    symbol = lt.symbol()
    assert symbol == MARKET_SYMBOL, f"Market {MARKET_ID} is {symbol}, expected {MARKET_SYMBOL}"
    assert pool.address == POOL, f"Market {MARKET_ID} cryptopool is {pool.address}, expected {POOL}"

    print(f"Market {MARKET_ID} ({symbol}), LT {lt.address}, AMM {amm.address}")
    print(f"  cryptopool:  {pool.address}")
    print(f"  POLICY():    {pool.POLICY()} -> {NEW_POLICY}")
    print(f"  RPF:         {pool.reserved_profit_fraction() / 1e10:.2%} -> {NEW_RPF / 1e10:.2%} (by Curve)")
    print(f"  rate:        {amm.rate()} -> {RATE}  "
          f"({amm.rate() * 365 * 86400 / 1e18:.4%} -> {RATE * 365 * 86400 / 1e18:.4%} per year)")
    print(f"  AMM fee:     {amm.fee() / 1e18:.4%} -> {FEE / 1e18:.4%}")

    # Guard: revert unless the underlying Curve pool already runs the policy contract which Curve ownership vote
    # 1508 installs, i.e. unless that vote has executed.
    actions = [
        Action(to=comparator.address, value=0,
               data=comparator.check_equal.prepare_calldata(pool.address, POLICY_SELECTOR, int(NEW_POLICY, 16))),
        Action(to=factory_owner.address, value=0,
               data=factory_owner.lt_set_rate.prepare_calldata(lt.address, RATE)),
        Action(to=factory_owner.address, value=0,
               data=factory_owner.lt_set_amm_fee.prepare_calldata(lt.address, FEE)),
    ]

    title = 'Enable permissionless propAMM operation for yb-WETH market (10)'

    summary = (
        'Research in [https://forum.yieldbasis.com/t/oracle-reports-for-yieldbasis-pools-a-new-price-scale-and-fee-policy-for-the-weth-market/40] '
        'describes the approach and simulations. '
        'Curve vote 1508 [https://www.curve.finance/dao/ethereum/proposals/1508-ownership] sets necessary pool '
        'parameters on Curve side. '
        'On YB side, we set donation APY to 2.60% and releverage fee to 150 bps. '
        'Our vote is only executable once Curve vote is executed.'
    )

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
        expected_policy_call = SET_POLICY_SELECTOR + bytes(12) + bytes.fromhex(NEW_POLICY[2:])
        expected_rpf_call = SET_FEE_PARAMETERS_SELECTOR + NEW_RPF.to_bytes(32, 'big') + bytes(32)
        assert bytes.fromhex(pool.address[2:]) in curve_script, \
            f"Curve vote {CURVE_VOTE_ID} does not touch pool {pool.address}"
        assert expected_policy_call in curve_script, f"Curve vote {CURVE_VOTE_ID} does not install policy {NEW_POLICY}"
        assert expected_rpf_call in curve_script, f"Curve vote {CURVE_VOTE_ID} does not set RPF to {NEW_RPF / 1e10:.2%}"
        print(f"\nCurve vote {CURVE_VOTE_ID} installs the expected policy and RPF")

        print(f"\n=== YB vote before Curve vote {CURVE_VOTE_ID}: must revert ===")
        assert pool.POLICY() != NEW_POLICY, "Pool already runs the new policy - guard is a no-op"
        with boa.env.anchor():
            try:
                with boa.env.prank(DAO):
                    for action in actions:
                        boa.env.raw_call(to_address=action.to, data=action.data)
                raise SystemExit("ERROR: YB vote executed while the policy is not in place!")
            except Exception as e:
                print(f"  correctly reverted: {revert_reason(e)}")

        print(f"\n=== Executing Curve ownership vote {CURVE_VOTE_ID} via Convex ===")
        execute_curve_vote(curve_voting, CURVE_VOTE_ID)
        print(f"  pool POLICY() = {pool.POLICY()}, RPF = {pool.reserved_profit_fraction() / 1e10:.2%}")
        assert pool.POLICY() == NEW_POLICY, "Curve vote did not install the expected policy"
        assert pool.reserved_profit_fraction() == NEW_RPF, "Curve vote did not set the expected RPF"

        print(f"\n=== YB vote after Curve vote {CURVE_VOTE_ID} ===")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                boa.env.raw_call(to_address=action.to, data=action.data)
                print(f"  action {i + 1} out of {len(actions)}")

        print(f"  market {MARKET_ID}: rate {amm.rate()} ({amm.rate() * 365 * 86400 / 1e18:.4%} per year), "
              f"AMM fee {amm.fee() / 1e18:.4%}")
        assert amm.rate() == RATE, "Rate not updated as expected"
        assert amm.fee() == FEE, "Fee not updated as expected"

        print("\n=== LT under the new policy ===")
        boa.env.time_travel(seconds=86400)
        pool_value = pool.totalSupply() * pool.lp_price() // 10**18
        donation_apy = RATE * 365 * 86400 * amm.get_debt() // pool_value / 1e18
        print(f"  pool donation APY at this debt / pool value: {donation_apy:.4%}")

        donation_shares = pool.donation_shares()
        lt.distribute_borrower_fees()
        print(f"  distribute_borrower_fees: donation shares {donation_shares / 1e18:,.4f} -> "
              f"{pool.donation_shares() / 1e18:,.4f}")
        assert pool.donation_shares() > donation_shares, "Borrower fees were not donated"

        weth = boa.loads_abi(WETH_ABI, name="WETH").at(WETH)
        assets = 10 * 10**18
        debt = proportional_debt(amm, pool, assets)
        boa.env.set_balance(DEPOSITOR, assets + 10**18)
        with boa.env.prank(DEPOSITOR):
            weth.deposit(value=assets)
            weth.approve(lt.address, 2**256 - 1)
            shares = lt.deposit(assets, debt, 0)
            print(f"  deposit {assets / 1e18:,.2f} WETH with {debt / 1e18:,.2f} crvUSD debt -> {shares / 1e18:,.4f} shares")
            withdrawn = lt.withdraw(shares, 0)
            print(f"  withdraw {shares / 1e18:,.4f} shares -> {withdrawn / 1e18:,.4f} WETH")
        assert withdrawn > assets * 99 // 100, "Withdrawal lost more than 1%"
        print("OK")
