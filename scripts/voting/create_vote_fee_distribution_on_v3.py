#!/usr/bin/env python3
"""
Aragon-OSx vote: extend FeeDistributor's token set to cover YB v3 markets
7, 8, 9, 10 alongside the existing 3-6.

Single action: fee_distributor.add_token_set(union) where `union` is the
existing current token set plus the 4 new LTs (markets 7-10) and any new
underlying assets not already in the set. add_token_set increments
current_token_set, so this REGISTERS a new set and switches distribution
to it without disabling the old (old sets remain in token_sets[id]).

Pre-checks (build-time, not in the vote):
  * factory.fee_receiver() == FEE_DISTRIBUTOR (already wired earlier)
  * fee_distributor.owner() == DAO (so the DAO can call add_token_set)

Does NOT call withdraw_admin_fees and does NOT re-set fee_receiver — the
former can be done permissionlessly later, the latter is already in place.

Usage (from project root):
  python scripts/voting/create_vote_fee_distribution_on_v3.py           # production
  python scripts/voting/create_vote_fee_distribution_on_v3.py --fork    # forked dry-run
"""
import os
import sys
import json
import boa
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK, PINATA_TOKEN

FORK = "--fork" in sys.argv[1:]

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
FEE_DISTRIBUTOR = "0xD11b416573EbC59b6B2387DA0D2c0D1b3b1F7A90"

# Markets covered by THIS vote (the new v3 LTs).
NEW_MARKET_IDS = [7, 8, 9, 10]

PROPOSER_ACCOUNT = "yb-deployer-2"

Proposal = namedtuple("Proposal",
                      ["metadata", "actions", "allowFailureMap", "startDate",
                       "endDate", "voteOption", "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])


def pin_to_ipfs(content: dict) -> str:
    url = "https://api.pinata.cloud/pinning/pinJSONToIPFS"
    headers = {"Authorization": f"Bearer {PINATA_TOKEN}",
               "Content-Type": "application/json"}
    payload = {"pinataContent": content,
               "pinataMetadata": {"name": "pinnie.json"},
               "pinataOptions": {"cidVersion": 1}}
    r = requests.post(url, json=payload, headers=headers, timeout=30)
    assert 200 <= r.status_code < 400, r.text
    return "ipfs://" + r.json()["IpfsHash"]


def keystore_address(fname: str) -> str:
    """Read the EOA address from a brownie keystore without decrypting —
    the address is stored in plaintext alongside the encrypted key. Used in
    fork mode so the prank EOA matches the production proposer."""
    path = os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", fname + ".json"))
    with open(path) as f:
        return "0x" + json.load(f)["address"]


def account_load(fname: str):
    path = os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", fname + ".json"))
    with open(path) as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
    return account.Account.from_key(pkey)


def read_current_token_set(fee_distributor, erc20) -> list:
    """Pull every token in fee_distributor's current set. token_sets is a
    DynArray, so we read indices until the call reverts/returns zero — easier
    to just rely on the DynArray length view if Vyper exposes one, but here
    we walk indices defensively."""
    set_id = fee_distributor.current_token_set()
    tokens = []
    i = 0
    while True:
        try:
            t = fee_distributor.token_sets(set_id, i)
        except Exception:
            break
        if int(t, 16) == 0:
            break
        tokens.append(t)
        i += 1
    return tokens


if __name__ == "__main__":
    if FORK:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = keystore_address(PROPOSER_ACCOUNT)
    else:
        boa.set_network_env(NETWORK)
        signer = account_load(PROPOSER_ACCOUNT)
        boa.env.add_account(signer, force_eoa=True)

    voting = boa.load_abi(
        os.path.dirname(__file__) + "/TokenVoting.abi.json",
        name="AragonVoting").at(VOTING_PLUGIN)
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    fee_distributor = boa.load_partial(
        "contracts/dao/FeeDistributor.vy").at(FEE_DISTRIBUTOR)
    lt_interface = boa.load_partial("contracts/LT.vy")
    erc20 = boa.load_abi(os.path.dirname(__file__) + "/erc20.abi.json")

    # --- pre-checks --------------------------------------------------------
    assert factory.fee_receiver().lower() == FEE_DISTRIBUTOR.lower(), (
        f"factory.fee_receiver() = {factory.fee_receiver()}, expected "
        f"FEE_DISTRIBUTOR {FEE_DISTRIBUTOR}")
    assert fee_distributor.owner().lower() == DAO.lower(), (
        f"fee_distributor.owner() = {fee_distributor.owner()}, expected DAO")

    # --- build the new token set ------------------------------------------
    current = read_current_token_set(fee_distributor, erc20)
    current_lower = {a.lower() for a in current}
    print(f"current token_set ({fee_distributor.current_token_set()}): "
          f"{len(current)} tokens")
    for t in current:
        try:
            print(f"  - {t}  ({erc20.at(t).symbol()})")
        except Exception:
            print(f"  - {t}")

    # LTs only — the current on-chain set was deliberately pared down to LTs
    # (no underlying assets). Distribute fees as yb-* LT shares only.
    new_lts = [factory.markets(i).lt for i in NEW_MARKET_IDS]

    additions = []
    for addr in new_lts:
        if addr.lower() in current_lower:
            continue
        if addr.lower() in {a.lower() for a in additions}:
            continue
        additions.append(addr)

    print(f"\nadding {len(additions)} new LTs:")
    for t in additions:
        print(f"  + {t}  ({erc20.at(t).symbol()})")

    new_set = list(current) + additions
    assert len(new_set) <= 64, (
        f"new token_set has {len(new_set)} entries; check MAX_TOKENS")

    # --- single vote action -----------------------------------------------
    actions = [
        Action(to=fee_distributor.address, value=0,
               data=fee_distributor.add_token_set.prepare_calldata(new_set)),
    ]

    metadata = pin_to_ipfs({
        "title": "Turn fee distribution ON for YB v3 markets (7-10)",
        "summary": (
            "Extend FeeDistributor's token set to also distribute fees from "
            "YB v3 markets " + ", ".join(f"#{i}" for i in NEW_MARKET_IDS) +
            f". add_token_set publishes a new set containing the existing "
            f"{len(current)} LTs plus {len(additions)} new LTs."
        ),
        "resources": [],
    }).encode() if not FORK else b""

    proposal_id = voting.createProposal(*Proposal(
        metadata=metadata, actions=actions, allowFailureMap=0,
        startDate=0, endDate=0, voteOption=0, tryEarlyExecution=True))
    print(f"\nproposalId = {proposal_id}")

    # --- forked simulation ------------------------------------------------
    if FORK:
        print("\nSimulating execution as DAO…")
        before_id = fee_distributor.current_token_set()
        with boa.env.prank(DAO):
            for action in actions:
                boa.env.raw_call(to_address=action.to, data=action.data)
        after_id = fee_distributor.current_token_set()
        assert after_id == before_id + 1, (
            f"current_token_set did not advance ({before_id} -> {after_id})")
        on_chain = read_current_token_set(fee_distributor, erc20)
        print(f"current_token_set: {before_id} -> {after_id}, "
              f"{len(on_chain)} tokens")
        on_chain_lower = {a.lower() for a in on_chain}
        for t in new_set:
            assert t.lower() in on_chain_lower, (
                f"{t} missing from new token set")
        print("New set contains every expected token.")
