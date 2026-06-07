#!/usr/bin/env python3
"""
Increase the crvUSD allocation of the YB WETH pool (market id 10) so its deposit
cap grows by ~$5M, because that pool has been performing particularly well.

Mechanism
---------
A YB pool's deposit cap is enforced by its crvUSD allocation. LT.deposit()
requires, after the deposit:

    amm.max_debt() // 2 >= value          (value == AMM equity oracle, ~half of LP collateral value)

and amm.max_debt() == (crvUSD sitting in the AMM) + debt == lt.stablecoin_allocated().

When a user brings collateral worth $X, the LT borrows ~$X of crvUSD and adds
BOTH legs to the cryptopool, so the AMM's LP collateral grows by ~$2X while the
equity ("value") grows by ~$X. Hence the deposit headroom in *collateral value*
that an allocation A allows above the current collateral C is:

    headroom == A - C          (max collateral value == allocation)

equivalently ~ (A - C) / 2 of fresh *equity* / deposited collateral value.

So to add ~$5M of deposit headroom we set

    new_allocation = current_collateral_value + 10_000_000 crvUSD

where current_collateral_value = lp_price * amm.collateral_amount() (the same
quantity HybridFactoryOwner uses for its allocation floor). This pulls the extra
~10M crvUSD from the Factory's reserve into the pool's AMM.

The allocation is set through the current Factory owner (HybridFactoryOwner at
factory.admin()), whose lt_allocate_stablecoins(lt, limit) is ADMIN-only and the
ADMIN is the YB DAO, so this REQUIRES A DAO VOTE. The owner clamps the limit up
to 95% of collateral value; our target is far above that floor, so it is applied
verbatim.

Usage:
  python scripts/voting/create_vote_pool10_weth_up.py          # prod: create the veYB vote
  python scripts/voting/create_vote_pool10_weth_up.py --test   # fork: simulate execution and
                                                               #   verify $4M WETH deposits / $6M reverts
"""
import os
import sys
import json
import requests
from collections import namedtuple
from getpass import getpass

import boa
from boa.contracts.abi.abi_contract import ABIContractFactory
from eth_account import account

from networks import NETWORK
from networks import PINATA_TOKEN

# --- on-chain constants ------------------------------------------------------

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"  # YB DAO == HybridFactoryOwner.ADMIN()

MARKET_ID = 10                       # yb-WETH pool
EXTRA_CRVUSD = 10_000_000 * 10**18   # 10M crvUSD above current collateral value -> ~$5M extra cap

PROPOSER_KEYSTORE = "yb-deployer"    # TokenVoting enforces a 1-day per-proposer cooldown

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])

# Minimal ABIs for fork-mode verification
_ERC20_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "u", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "deposit", "type": "function", "stateMutability": "payable", "inputs": [], "outputs": []},
]
_CRYPTOPOOL_ABI = [
    {"name": "price_scale", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint256"}]},
]


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
    factory = boa.load_partial('contracts/Factory.vy').at(FACTORY)
    factory_owner = boa.load_partial('contracts/HybridFactoryOwner.vy').at(factory.admin())
    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)
    market = factory.markets(MARKET_ID)
    lt = boa.load_partial('contracts/LT.vy').at(market.lt)
    amm = boa.load_partial('contracts/AMM.vy').at(market.amm)
    price_oracle = boa.load_partial('contracts/CryptopoolLPOracle.vy').at(market.price_oracle)
    return factory, factory_owner, voting, market, lt, amm, price_oracle


def collateral_value(amm, price_oracle):
    """Value of the LP collateral the AMM currently holds, in crvUSD (1e18)."""
    return price_oracle.price() * amm.collateral_amount() // 10**18


def build_action(factory_owner, lt, new_allocation):
    return Action(
        to=factory_owner.address, value=0,
        data=factory_owner.lt_allocate_stablecoins.prepare_calldata(lt.address, new_allocation))


def vote_metadata(coll_value, new_allocation) -> dict:
    return {
        'title': 'Increase crvUSD allocation for the WETH pool (market 10) by ~$5M',
        'summary': (
            'The yb-WETH pool (market id 10) has performed particularly well, so this raises its '
            'crvUSD allocation to 10M above the pool\'s current collateral value '
            f'({coll_value // 10**18:,} crvUSD), i.e. to {new_allocation // 10**18:,} crvUSD. '
            'A YB pool can hold collateral value up to its crvUSD allocation, and each $1 of '
            'deposited collateral consumes ~$1 of that headroom (the other ~$1 is the borrowed '
            'crvUSD leg), so allocating 10M above the current collateral opens ~$5M of new deposit '
            'capacity. The extra crvUSD is drawn from the Factory reserve into the pool\'s AMM via '
            'HybridFactoryOwner.lt_allocate_stablecoins.'),
        'resources': [],
    }


# --- fork-mode verification --------------------------------------------------

def _deposit_value_usd(lt, market, user, usd):
    """Deposit WETH worth ~$usd directly into the LT (no HybridVault).

    The user supplies only the WETH leg; the crvUSD debt leg comes from the AMM's
    allocated reserves. Returns the shares minted.
    """
    cryptopool = ABIContractFactory.from_abi_dict(_CRYPTOPOOL_ABI, name="CP").at(market.cryptopool)
    weth = ABIContractFactory.from_abi_dict(_ERC20_ABI, name="WETH").at(WETH)

    price_scale = cryptopool.price_scale()                 # crvUSD per WETH (1e18)
    debt = usd * 10**18                                     # crvUSD leg, ~$usd
    assets = usd * 10**18 * 10**18 // price_scale           # WETH leg, ~$usd worth

    # Wrap ETH -> WETH for the user, then approve the LT.
    boa.env.set_balance(user, assets + 10**18)
    with boa.env.prank(user):
        weth.deposit(value=assets)
        weth.approve(lt.address, 2**256 - 1)
        return lt.deposit(assets, debt, 0, user)


def run_test():
    boa.fork(NETWORK, block_identifier="latest")
    factory, factory_owner, voting, market, lt, amm, price_oracle = load_contracts()

    assert factory_owner.ADMIN().lower() == DAO.lower(), "factory owner ADMIN is not the DAO"
    assert lt.symbol() == "yb-WETH", f"market {MARKET_ID} is not the WETH pool ({lt.symbol()})"

    coll_value = collateral_value(amm, price_oracle)
    new_allocation = coll_value + EXTRA_CRVUSD

    print(f"fork block               : {boa.env.evm.patch.block_number}")
    print(f"factory owner            : {factory_owner.address}")
    print(f"market {MARKET_ID} lt / amm     : {lt.address} / {amm.address}")
    print(f"collateral value         : {coll_value / 1e18:,.0f} crvUSD")
    print(f"current allocation       : {lt.stablecoin_allocation() / 1e18:,.0f} crvUSD")
    print(f"-> new allocation        : {new_allocation / 1e18:,.0f} crvUSD")

    value_before = amm.value_oracle().value
    cap_before = amm.max_debt() // 2
    print(f"\nbefore: equity value={value_before / 1e18:,.0f}  cap(max_debt/2)={cap_before / 1e18:,.0f}  "
          f"headroom={(cap_before - value_before) / 1e18:,.0f}")

    # Simulate the vote execution: the DAO calls the proposal action calldata.
    action = build_action(factory_owner, lt, new_allocation)
    with boa.env.prank(DAO):
        boa.env.raw_call(to_address=action.to, data=action.data)

    assert lt.stablecoin_allocation() == new_allocation, "allocation not applied"
    value_after = amm.value_oracle().value
    cap_after = amm.max_debt() // 2
    headroom = cap_after - value_after
    print(f"after : equity value={value_after / 1e18:,.0f}  cap(max_debt/2)={cap_after / 1e18:,.0f}  "
          f"headroom={headroom / 1e18:,.0f} crvUSD")
    assert 4_000_000 * 10**18 < headroom < 6_000_000 * 10**18, "deposit headroom is not ~$5M"

    # Verify deposit behaviour directly against the LT (no HybridVault), each on a
    # clean snapshot of the post-allocation state.
    print("\n--- direct LT deposit checks (no HybridVault) ---")
    with boa.env.anchor():
        shares = _deposit_value_usd(lt, market, "0x1111111111111111111111111111111111111111", 4_000_000)
        print(f"$4M WETH deposit  -> OK, minted {shares / 1e18:,.4f} yb-WETH shares")
        assert shares > 0

    with boa.env.anchor():
        with boa.reverts("Debt too high"):
            _deposit_value_usd(lt, market, "0x2222222222222222222222222222222222222222", 6_000_000)
        print("$6M WETH deposit  -> correctly reverted 'Debt too high'")

    print("\nPASS: +$5M cap opens a $4M WETH deposit but blocks $6M.")


def run_prod():
    boa.set_network_env(NETWORK)
    proposer = account_load(PROPOSER_KEYSTORE)
    boa.env.add_account(proposer)

    factory, factory_owner, voting, market, lt, amm, price_oracle = load_contracts()
    assert lt.symbol() == "yb-WETH", f"market {MARKET_ID} is not the WETH pool ({lt.symbol()})"

    coll_value = collateral_value(amm, price_oracle)
    new_allocation = coll_value + EXTRA_CRVUSD

    print(f"factory owner      : {factory_owner.address}")
    print(f"WETH lt / amm      : {lt.address} / {amm.address}")
    print(f"collateral value   : {coll_value / 1e18:,.0f} crvUSD")
    print(f"current allocation : {lt.stablecoin_allocation() / 1e18:,.0f} crvUSD")
    print(f"-> new allocation  : {new_allocation / 1e18:,.0f} crvUSD")

    proposal_id = voting.createProposal(*Proposal(
        metadata=pin_to_ipfs(vote_metadata(coll_value, new_allocation)).encode(),
        actions=[build_action(factory_owner, lt, new_allocation)],
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
