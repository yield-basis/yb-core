#!/usr/bin/env python3
"""
Create a single DAO vote that wires the MerklPIDDriver to Merkl and launches a long-running,
dynamic FIX_APR crvUSD incentive campaign on the YB sink pool - all executed atomically when the
vote passes.

The proposal runs three actions on the driver (DAO is the driver's owner, so it can do all three):
  1. set_merkl(DC, wrapper)   - install Merkl's DistributionCreator + the crvUSD PullTokenWrapper
  2. accept_conditions()      - sign Merkl's terms so createCampaign's hasSigned gate passes
  3. create_campaign(amount, 18, 0, duration, campaign_data)

start_timestamp is 0 on purpose: the driver stores block.timestamp, so the campaign begins the
moment the vote executes - no timestamp to pin. campaign_data is fetched from Merkl's encode API
for the *exact* on-chain params (creator=driver, rewardToken=wrapper, amount, start=0, duration),
so it is consistent by construction; the start=0 variant does not depend on execution time, which
is what makes bundling into a vote safe.

MAINNET PREREQUISITES (Merkl side - set on the DistributionCreator by a Merkl guardian):
  - rewardTokenMinAmounts(wrapper) > 0            (whitelist the wrapper reward token)   [done]
  - feeRebate(driver) == 1e9 (100%)              (else the 3% creation fee is pulled in
                                                   crvUSD on the FULL amount -> 1B reverts)
FORK mode grants both in-fork (guardian prank) so the end-to-end simulation runs green; the real
vote will only execute successfully once they are set on mainnet.

    python scripts/voting/create_vote_merkl_campaign.py
"""

import boa
import os
import json
import requests

from time import sleep
from eth_account import account
from eth_utils import keccak
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN
from networks import ETHERSCAN_API_KEY
from boa.explorer import Etherscan
from boa.verifiers import verify as boa_verify


FORK = True
EXTRA_TIMEOUT = 10
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"

# Whale with voting power, used only to simulate createProposal in fork mode. In production the
# proposal is created by the yb-deployer account (which is also the driver's manager).
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

# Merkl encode API: turns a campaign config into the on-chain campaign_data bytes.
MERKL_ENCODE_URL = "https://api.merkl.xyz/v4/config/encode/batch"

# Campaign parameters. amount is the wrapper cap (crvUSD only leaves as users claim; the FIX_APR
# controller bounds real distribution). Over-sizing is free ONLY with the 100% fee rebate.
AMOUNT = 1_000_000_000 * 10**18     # 1B crvUSD cap
DURATION = 365 * 86400              # 1 year
START = 0                           # 0 -> starts at the block the vote executes in
CAMPAIGN_TYPE = 18

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])


def pin_to_ipfs(content: dict):
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {"Authorization": f"Bearer {PINATA_TOKEN}", "Content-Type": "application/json"}
    payload = {"pinataContent": content, "pinataMetadata": {"name": "pinnie.json"}, "pinataOptions": {"cidVersion": 1}}
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


def fetch_campaign_data(cfg, driver, wrapper, sink, amount, start, duration):
    """Ask Merkl's encode API for the campaign_data bytes matching the exact on-chain params."""
    config = {
        "distributionChainId": 1,
        "amount": str(amount),
        "computeChainId": 1,
        "creator": driver,          # must match on-chain creator (driver); commits into the hash
        "rewardToken": wrapper,     # must match on-chain reward token (the wrapper)
        "forwardingList": [],
        "forwardingEnabled": True,
        "computeScoreParameters": {"computeMethod": "genericTimeWeighted"},
        "distributionMethodParameters": {
            "distributionMethod": "COMPOSED",
            "distributionSettings": {
                "adapters": [{"key": "yb", "name": "yieldBasisPID", "params": {"driver": driver}}],
                "computeExpression": "yb",
                "rewardTokenPricing": True,
                "targetTokenPricing": True,
                "targetDistributionMethod": "FIX_APR",
            },
        },
        "campaignType": CAMPAIGN_TYPE,
        "blacklist": [], "whitelist": [], "forwarders": [],
        "targetToken": sink,
        "startTimestamp": start,
        "endTimestamp": start + duration,
    }
    resp = requests.post(MERKL_ENCODE_URL, json=[config], headers={"Content-Type": "application/json"}, timeout=30)
    resp.raise_for_status()
    p0 = resp.json()["payloads"][0]
    assert p0.get("error") is None, f"Merkl encode error: {p0['error']}"
    args = p0["args"]
    assert int(args["startTimestamp"]) == start and int(args["duration"]) == duration, args
    assert args["creator"].lower() == driver.lower() and args["rewardToken"].lower() == wrapper.lower(), args
    return bytes.fromhex(args["campaignData"].removeprefix("0x"))


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    cfg = json.load(open(os.path.join(os.path.dirname(__file__), '..', 'merkl_pid_deployment.json')))
    DRIVER = cfg["merkl_pid_driver"]
    DC = cfg["distribution_creator"]
    WRAPPER = cfg["merkl_wrapper"]
    SINK = cfg["sink_lp"]
    CRVUSD = cfg["crvusd"]

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    driver = boa.load_partial('contracts/net_pressure/MerklPIDDriver.vy').at(DRIVER)
    dc = boa.loads_abi(json.dumps([
        {"name": "accessControlManager", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
        {"name": "setUserFeeRebate", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": []},
        {"name": "rewardTokenMinAmounts", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
        {"name": "feeRebate", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    ])).at(DC)

    # Live Merkl-side prerequisites, set by a Merkl guardian on the DistributionCreator:
    #   rewardTokenMinAmounts(wrapper) > 0  -> the wrapper reward token is whitelisted
    #   feeRebate(driver) == 1e9 (100%)     -> creation fee waived ("invoice afterward"); without
    #     it, 3% of the FULL cap is pulled in crvUSD at creation, so the 1B cap reverts.
    min_amount = dc.rewardTokenMinAmounts(WRAPPER)
    rebate = dc.feeRebate(DRIVER)
    whitelisted = min_amount > 0
    rebated = rebate == 10**9
    print("Merkl prerequisites (live on-chain):")
    print(f"  wrapper whitelisted    : {'READY' if whitelisted else 'NOT READY'}  (rewardTokenMinAmounts={min_amount})")
    print(f"  driver 100% fee rebate : {'READY' if rebated else 'NOT READY'}  (feeRebate={rebate}, need {10**9})")
    if not (whitelisted and rebated):
        print("  NOTE: vote execution reverts until Merkl sets the missing prerequisite(s).")

    campaign_data = fetch_campaign_data(cfg, DRIVER, WRAPPER, SINK, AMOUNT, START, DURATION)
    print(f"campaign_data (start={START}, amount={AMOUNT/1e18:,.0f}, duration={DURATION}s): 0x{campaign_data.hex()}")

    actions = [
        Action(to=DRIVER, value=0, data=driver.set_merkl.prepare_calldata(DC, WRAPPER)),
        Action(to=DRIVER, value=0, data=driver.accept_conditions.prepare_calldata()),
        Action(to=DRIVER, value=0, data=driver.create_campaign.prepare_calldata(AMOUNT, CAMPAIGN_TYPE, START, DURATION, campaign_data)),
    ]

    if not FORK:
        proposal_id = voting.createProposal(*Proposal(
            metadata=pin_to_ipfs({
                'title': 'Launch Merkl PID dynamic crvUSD incentives (start now, 1 year)',
                'summary': 'Wire the MerklPIDDriver to Merkl (set_merkl + accept_conditions) and open a 1-year dynamic '
                           'FIX_APR crvUSD campaign on the YB sink pool, driven by the on-chain net-pressure PID. Reward '
                           'token is the whitelisted crvUSD PullTokenWrapper; crvUSD leaves the driver reserve only as '
                           'users claim, so the 1B wrapper cap is a ceiling, not a lockup. Starts the moment this executes.',
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
        # Fork: skip createProposal (proposer voting-power is environmental); validate that the
        # DAO executing the three actions wires Merkl and opens the campaign. If the fee rebate is
        # still pending on this node, grant it in-fork (guardian) purely so the sim can reach
        # create_campaign; once Merkl sets it on mainnet this branch is skipped and the sim runs
        # against real state. The wrapper whitelisting is already live, so it is asserted, not faked.
        print("Simulating execution (DAO runs the proposal actions)")
        if not rebated:
            acm = boa.loads_abi(json.dumps([
                {"name": "getRoleMember", "stateMutability": "view", "type": "function", "inputs": [{"type": "bytes32"}, {"type": "uint256"}], "outputs": [{"type": "address"}]},
            ])).at(dc.accessControlManager())
            with boa.env.prank(acm.getRoleMember(keccak(text="GUARDIAN_ROLE"), 0)):
                dc.setUserFeeRebate(DRIVER, 10**9)
            print("  [sim] granted feeRebate(driver)=1e9 in-fork (pending on mainnet)")
        assert dc.rewardTokenMinAmounts(WRAPPER) > 0, "wrapper not whitelisted on mainnet"

        erc20 = boa.loads_abi(json.dumps([
            {"name": "balanceOf", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
        ])).at(CRVUSD)
        bal0 = erc20.balanceOf(DRIVER)

        cid = None
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                ret = boa.env.raw_call(to_address=action.to, data=action.data)
                if i == 2:
                    cid = ret.output[-32:]
                print(f"  action {i + 1}/{len(actions)} executed")

        print(f"  merkl_creator : {driver.merkl_creator()}")
        print(f"  reward_wrapper: {driver.reward_wrapper()}")
        print(f"  campaign id   : 0x{cid.hex()}")
        print(f"  crvUSD fee paid: {(bal0 - erc20.balanceOf(DRIVER)) / 1e18:,.2f} (0 with the 100% rebate)")
        assert driver.merkl_creator().lower() == DC.lower()
        assert driver.reward_wrapper().lower() == WRAPPER.lower()
        assert cid != b"\x00" * 32, "campaign not created"
        print("OK")
