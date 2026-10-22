#!/usr/bin/env python3
"""
Raise the deposit caps of the WBTC (ID=7) and cbBTC (ID=8) markets to $50M each.

A market's cap is enforced by its crvUSD allocation: LT.deposit() requires

    amm.max_debt() // 2 >= value        (value == AMM equity oracle == deposited USD value)

and amm.max_debt() == crvUSD sitting in the AMM + debt == lt.stablecoin_allocated().
Because the market runs at 2x leverage, every $1 of deposit is backed by $2 of crvUSD
allocation, so

    cap ($) = crvUSD allocation / 2   =>   $50M cap needs a $100M allocation.

Setting the allocation pulls the difference out of the Factory's crvUSD reserve into
the market's AMM. The allocation is set through the current Factory owner
(HybridFactoryOwner at factory.admin()), whose lt_allocate_stablecoins(lt, limit) is
ADMIN-only with the YB DAO as ADMIN, hence this vote.

Both markets are currently at (or over) their cap, so today they take no new deposits:
  market 7 (yb-WBTC):  allocation ~$64.0M  -> cap ~$32.0M, value ~$31.5M
  market 8 (yb-cbBTC): allocation ~$87.1M  -> cap ~$43.5M, value ~$46.9M  (over cap)

FORK mode executes the proposal on a fork at head and checks that both allocations are
really funded from the Factory reserve and that both markets end up with positive
deposit headroom.

    python scripts/voting/create_vote_wbtc_cbbtc_cap_50m.py
"""

import boa
import os
import json
import requests

from eth_account import account
from collections import namedtuple
from getpass import getpass
from networks import NETWORK
from networks import PINATA_TOKEN


MARKET_IDS = [7, 8]                                  # yb-WBTC, yb-cbBTC
MARKET_SYMBOLS = ['yb-WBTC', 'yb-cbBTC']

TARGET_CAP_USD = 50 * 10**6                          # $50M cap per market
NEW_ALLOCATION = 2 * TARGET_CAP_USD * 10**18         # cap = allocation / 2 at 2x leverage

FORK = False
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"

# Whale with voting power, used only to simulate createProposal in fork mode.
# In production the proposal is created by the yb-deployer account.
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"

Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate", "endDate", "voteOption",
                                   "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])


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
        pkey = account.Account.decrypt(json.load(f), getpass())
        return account.Account.from_key(pkey)


def market_state(lt, amm):
    """Cap, current equity value and remaining deposit headroom, all in 1e18-based USD."""
    cap = amm.max_debt() // 2
    value = amm.value_oracle().value
    return cap, value, cap - value


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
    crvusd = boa.load_abi(os.path.dirname(__file__) + '/erc20.abi.json', name="ERC20").at(CRVUSD)
    lt_deployer = boa.load_partial('contracts/LT.vy')
    amm_deployer = boa.load_partial('contracts/AMM.vy')

    markets = []
    total_increase = 0
    for market_id, expected_symbol in zip(MARKET_IDS, MARKET_SYMBOLS):
        market = factory.markets(market_id)
        lt = lt_deployer.at(market.lt)
        amm = amm_deployer.at(market.amm)

        symbol = lt.symbol()
        assert symbol == expected_symbol, f"Market {market_id} is {symbol}, expected {expected_symbol}"

        allocation = lt.stablecoin_allocation()
        assert allocation < NEW_ALLOCATION, f"Market {market_id} allocation is already {allocation / 1e18:,.0f}"
        total_increase += NEW_ALLOCATION - allocation
        markets.append({'id': market_id, 'lt': lt, 'amm': amm})

        cap, value, headroom = market_state(lt, amm)
        print(f"Market {market_id} ({symbol}), LT {lt.address}")
        print(f"  crvUSD allocation: {allocation / 1e18:>14,.0f} -> {NEW_ALLOCATION / 1e18:,.0f}")
        print(f"  cap ($):           {cap / 1e18:>14,.0f} -> {TARGET_CAP_USD:,.0f}")
        print(f"  value ($):         {value / 1e18:>14,.0f}   (headroom {headroom / 1e18:,.0f})")

    reserve = crvusd.balanceOf(FACTORY)
    print(f"\nFactory crvUSD reserve: {reserve / 1e18:,.0f}, needed: {total_increase / 1e18:,.0f}")
    assert reserve >= total_increase, "Not enough crvUSD in the Factory reserve"

    actions = [
        Action(to=factory_owner.address, value=0,
               data=factory_owner.lt_allocate_stablecoins.prepare_calldata(m['lt'].address, NEW_ALLOCATION))
        for m in markets
    ]

    title = 'Increase WBTC and cbBTC market caps to $50M'
    summary = ('Raise the deposit caps of the yb-WBTC (market 7) and yb-cbBTC (market 8) markets to $50M each, up '
               'from ~$32M and ~$43.5M. Both markets are currently at or above their cap and therefore accept no new '
               'deposits. Since a cap equals the market\'s crvUSD allocation divided by 2 (the markets run at 2x '
               'leverage), this sets the crvUSD allocation of each of the two markets to $100M, moving ~$36M and '
               '~$13M of crvUSD respectively out of the Factory reserve (~$717M) and into the markets.')

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
        print(f"\n=== Simulating execution of {len(actions)} actions ===")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                print(f"  action {i + 1} out of {len(actions)}")
                boa.env.raw_call(to_address=action.to, data=action.data)

        for m in markets:
            lt, amm = m['lt'], m['amm']
            allocation = lt.stablecoin_allocation()
            allocated = lt.stablecoin_allocated()
            cap, value, headroom = market_state(lt, amm)
            print(f"\nMarket {m['id']} ({lt.symbol()})")
            print(f"  crvUSD allocation: {allocation / 1e18:>14,.0f}")
            print(f"  crvUSD allocated:  {allocated / 1e18:>14,.0f}")
            print(f"  cap ($):           {cap / 1e18:>14,.0f}")
            print(f"  value ($):         {value / 1e18:>14,.0f}   (headroom {headroom / 1e18:,.0f})")
            assert allocation == NEW_ALLOCATION, "Allocation not updated as expected"
            assert allocated == NEW_ALLOCATION, "Allocation not funded from the Factory reserve"
            assert headroom > 0, "Market still has no deposit headroom"

        print(f"\nFactory crvUSD reserve: {crvusd.balanceOf(FACTORY) / 1e18:,.0f}")
        print("OK")
