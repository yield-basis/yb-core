#!/usr/bin/env python3
"""
Lower the HybridVaultFactory stablecoin_fraction from 55% to 45%.

What this changes
-----------------
stablecoin_fraction is the crvUSD/scrvUSD backing a HybridVault must hold per $1
of YB position value: HybridVault._downscale(x) = x * stablecoin_fraction / 1e18,
applied to the vault's share of each pool's amm.value_oracle().value. At 55% each
$1 of YB exposure needs $0.55 of scrvUSD held alongside it; at 45% it needs $0.45,
i.e. more capital efficiency for hybrid depositors.

set_stablecoin_fraction(frac) on the HybridVaultFactory is ADMIN-only and ADMIN is
the YB DAO directly (not the Factory owner), so the proposal action targets the
HybridVaultFactory itself and REQUIRES A DAO VOTE.

Usage:
  python scripts/voting/create_vote_hybrid_stablecoin_fraction_45.py          # prod: create the veYB vote
  python scripts/voting/create_vote_hybrid_stablecoin_fraction_45.py --test   # fork: simulate + verify
"""
import os
import sys
import json
import requests
from collections import namedtuple
from getpass import getpass

import boa
from eth_account import account

from networks import NETWORK
from networks import PINATA_TOKEN

# --- on-chain constants ------------------------------------------------------

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
HYBRID_VAULT_FACTORY = "0xBdC32268851C324c6185809271dfe6d8dab8dC5b"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"  # YB DAO == HybridVaultFactory.ADMIN()

CURRENT_FRACTION = 55 * 10**16   # 55%, for a sanity check at vote-build time
NEW_FRACTION = 45 * 10**16       # 45%

PROPOSER_KEYSTORE = "yb-deployer"  # TokenVoting enforces a 1-day per-proposer cooldown

# A few known HybridVaults with positions, used only by --test to show the effect.
DEMO_VAULTS = [
    "0x06533a7eCA685f872e7fA0a3f1CC98092f53e349",
    "0xb89469db9d9ebd94ca0Df9E4b5B386101b7fAA73",
    "0x97FC2fD53A3d4C035032927c4C4a4F07304bAddD",
    "0xf4ABF9aC5AD5d1B30d7023D12fec34729a080594",
]

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])


# --- helpers -----------------------------------------------------------------

def pin_to_ipfs(content: dict) -> str:
    response = requests.post(
        "https://api.pinata.cloud/pinning/pinJSONToIPFS",
        json={"pinataContent": content,
              "pinataMetadata": {"name": "pinnie.json"},
              "pinataOptions": {"cidVersion": 1}},
        headers={"Authorization": f"Bearer {PINATA_TOKEN}", "Content-Type": "application/json"},
        timeout=30,
    )
    assert 200 <= response.status_code < 400, response.text
    return "ipfs://" + response.json()["IpfsHash"]


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


def load_contracts():
    hvf = boa.load_partial('contracts/HybridVaultFactory.vy').at(HYBRID_VAULT_FACTORY)
    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    return hvf, voting


def build_action(hvf, new_fraction):
    return Action(
        to=hvf.address, value=0,
        data=hvf.set_stablecoin_fraction.prepare_calldata(new_fraction))


def vote_metadata(current_fraction, new_fraction) -> dict:
    return {
        'title': 'HybridVaults: lower stablecoin_fraction from 55% to 45%',
        'summary': (
            f'Set HybridVaultFactory.stablecoin_fraction from {current_fraction // 10**16}% to '
            f'{new_fraction // 10**16}%. This is the crvUSD/scrvUSD backing a HybridVault must hold '
            'per $1 of YB position value, so lowering it from 0.55 to 0.45 reduces the scrvUSD a '
            'hybrid depositor must keep alongside their YB exposure, improving capital efficiency. '
            'set_stablecoin_fraction is ADMIN-only on the HybridVaultFactory and ADMIN is the DAO.'),
        'resources': [],
    }


# --- fork-mode verification --------------------------------------------------

def _find_demo_vault():
    """Return (vault_contract, required_before) for the first demo vault with a position."""
    for addr in DEMO_VAULTS:
        try:
            v = boa.load_partial('contracts/HybridVault.vy').at(addr)
            req = v.required_crvusd()
            if req > 0:
                return v, req
        except Exception:
            continue
    return None, 0


def run_test():
    boa.fork(NETWORK, block_identifier="latest")
    hvf, voting = load_contracts()

    assert hvf.ADMIN().lower() == DAO.lower(), "HybridVaultFactory ADMIN is not the DAO"

    current = hvf.stablecoin_fraction()
    print(f"fork block          : {boa.env.evm.patch.block_number}")
    print(f"HybridVaultFactory  : {hvf.address}")
    print(f"current fraction    : {current} = {current / 1e16:.2f}%")
    print(f"-> new fraction     : {NEW_FRACTION} = {NEW_FRACTION / 1e16:.2f}%")
    if current != CURRENT_FRACTION:
        print(f"WARNING: current fraction is not the expected {CURRENT_FRACTION / 1e16:.0f}% "
              "- revisit before shipping")

    # Snapshot the backing requirement of a live vault before the change.
    vault, req_before = _find_demo_vault()

    # Simulate the vote execution: the DAO calls the proposal action calldata.
    action = build_action(hvf, NEW_FRACTION)
    with boa.env.prank(DAO):
        boa.env.raw_call(to_address=action.to, data=action.data)

    assert hvf.stablecoin_fraction() == NEW_FRACTION, "fraction not applied"
    print(f"\napplied fraction    : {hvf.stablecoin_fraction() / 1e16:.2f}%  (OK)")

    if vault is not None:
        req_after = vault.required_crvusd()
        ratio = req_after / req_before
        print(f"\ndemo vault {vault.address}")
        print(f"  required_crvusd before (55%): {req_before / 1e18:,.2f} crvUSD")
        print(f"  required_crvusd after  (45%): {req_after / 1e18:,.2f} crvUSD")
        print(f"  ratio after/before          : {ratio:.4f}  (expected ~{NEW_FRACTION / current:.4f})")
        assert abs(ratio - NEW_FRACTION / current) < 1e-3, "downscale did not track the new fraction"
        print("\nPASS: fraction flipped to 45% and a live vault's required backing dropped to 45/55.")
    else:
        print("\nNote: no demo vault with an open position found; fraction flip verified only.")
        print("PASS: fraction flipped to 45%.")


def run_prod():
    boa.set_network_env(NETWORK)
    proposer = account_load(PROPOSER_KEYSTORE)
    boa.env.add_account(proposer)

    hvf, voting = load_contracts()
    current = hvf.stablecoin_fraction()
    print(f"HybridVaultFactory : {hvf.address}")
    print(f"current fraction   : {current / 1e16:.2f}%")
    print(f"-> new fraction    : {NEW_FRACTION / 1e16:.2f}%")
    assert current == CURRENT_FRACTION, (
        f"current fraction {current / 1e16:.2f}% != expected {CURRENT_FRACTION / 1e16:.0f}%; "
        "review before creating the vote")

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs(vote_metadata(current, NEW_FRACTION)).encode(),
        actions=[build_action(hvf, NEW_FRACTION)],
        allowFailureMap=0,
        startDate=0,
        endDate=0,
        voteOption=0,
        tryEarlyExecution=True,
    ))
    print(f"Created proposal: {proposal_id}")


if __name__ == '__main__':
    if "--test" in sys.argv[1:]:
        run_test()
    else:
        run_prod()
