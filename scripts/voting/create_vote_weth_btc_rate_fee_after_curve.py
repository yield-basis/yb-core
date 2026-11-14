#!/usr/bin/env python3
"""
Set the donation APY and releverage fee of the WBTC (ID=7), cbBTC (ID=8), tBTC (ID=9) and WETH (ID=10)
markets, but only after the Curve ownership vote which attaches the new YBOraclizedPolicy contracts
(deployed 2026-11-14 from twocrypto-ng 24cb25e) to their cryptopools has been executed:

    market       pool donation APY   rate on crvUSD (2x)   releverage fee   Curve sets RPF
    7  yb-WBTC   0.90% -> 0.85%      1.80% -> 1.70%        120 -> 180 bps   30% -> 48.75%
    8  yb-cbBTC  0.90% -> 0.55%      1.80% -> 1.10%        120 -> 180 bps   30% -> 45%
    9  yb-tBTC   0.90% -> 0.85%      1.80% -> 1.70%        120 -> 180 bps   30% -> 48.75%
    10 yb-WETH   1.30% -> 0.90%      2.60% -> 1.80%        150 -> 140 bps   43% (unchanged)

The research (yb_pyth_policy_4pools.ipynb, curve-fx-sim finalists 2026-11-13) tunes the donation rates and
LevAMM releverage fees together with those policies and reserved profit fractions, so the new YB parameters
only make sense once the policies are live. The vote is therefore guarded: actions 1-4 ask the on-chain
CallComparator to assert that each pool's POLICY() is already the contract the Curve vote installs. Until it
executes, POLICY() is still the previous policy, the comparator reverts and the whole YB proposal reverts
atomically (it stays executable, so it can simply be re-executed once Curve's side lands). Should Curve
attach different policy contracts after all, this proposal can never execute and has to be recreated.

The Curve vote is not created yet. Until CURVE_VOTE_ID is filled in, --fork applies the notebook's actions
(set_fee_parameters where the RPF changes, then set_policy_contract) through the ownership agent and the
pools' AmmOwnerProxy. With CURVE_VOTE_ID set, it checks that the vote script carries those calls and
executes the vote with Convex' veCRV instead. Either way, on a fork at head:
  1. YB actions before Curve's vote -> must revert ("Not equal", i.e. the previous policies are in)
  2. Curve's actions                -> each POLICY() becomes the new YBOraclizedPolicy, RPFs set
  3. YB actions again               -> rates and AMM fees updated
  4. LT under the new policies, per market: borrower fees are donated, a deposit with debt at
     price_oracle fails the LT gate, a deposit through LTDepositZap and a withdrawal go through

    python scripts/voting/create_vote_weth_btc_rate_fee_after_curve.py           # create the vote
    python scripts/voting/create_vote_weth_btc_rate_fee_after_curve.py --fork    # simulate on a fork
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


Market = namedtuple("Market", ["id", "symbol", "pool", "policy", "rpf", "donation_bps", "fee_bps"])

# Per market: the cryptopool, the YBOraclizedPolicy which the Curve vote attaches to it (doubling as the
# guard value), the reserved profit fraction it sets (10**10 precision), the pool donation APY and the
# releverage fee
MARKETS = [
    Market(7, 'yb-WBTC', '0x313698667d7FDD6789a9BC70821309ff891E729A',
           '0x8c8CD70277b02E24aa9afE3dbAD1Ee10443eff2D', 4_875_000_000, 85, 180),
    Market(8, 'yb-cbBTC', '0x862CB4E988FB66E72f128d1183829f8c05B6c6A0',
           '0xe28A23C5BfD057dd0Ed398Fa6bb9d33ABb41c044', 4_500_000_000, 55, 180),
    Market(9, 'yb-tBTC', '0x4F52C3a81E33521e5a9A47FD9D3BE475D2279c2e',
           '0x4EacfB0B18302D8203ed1e203ba2fD9D5ecfa86C', 4_875_000_000, 85, 180),
    Market(10, 'yb-WETH', '0x656341Ef90b622c6634e0573772FfB7f3669b9f3',
           '0xC4561F25034Bc70CED5f034E863dD150f2bA763a', 4_300_000_000, 90, 140),
]


def market_rate(m: Market) -> int:
    # 2x leverage: the rate on the LevAMM's crvUSD debt is twice the pool donation APY
    return 2 * m.donation_bps * 10**18 // 10_000 // (365 * 86400)


def market_fee(m: Market) -> int:
    return m.fee_bps * 10**18 // 10_000


# As in the notebook: WBTC and tBTC 539066463, cbBTC 348807711, WETH 570776255
assert [market_rate(m) for m in MARKETS] == [539066463, 348807711, 539066463, 570776255]

FORK = "--fork" in sys.argv[1:]

VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CALL_COMPARATOR = "0xd3BFa85dc668Aab38121bE12D69dd180301dec25"
DEPOSIT_ZAP = "0xF3a2bf354C28a1e0C84033f3ba3379e223F56E6A"  # LTDepositZap, fork mode only
DEPOSIT_USD = 100_000                                      # fork mode only

# Curve DAO (ownership) - only used by the fork simulation to land Curve's side before ours
CURVE_VOTE_ID = None  # Not created yet
CURVE_VOTING = "0xE478de485ad2fe566d49342Cbd03E49ed7DB3356"
CURVE_AGENT = "0x40907540d8a6C65c637785e8f8B742ae6b0b9968"      # ownership agent
AMM_OWNER_PROXY = "0xeDA0108D5bB8cdE2ba4A59E22F3Ed5d211C77187"  # twocrypto factory admin, owned by the agent
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

AMM_OWNER_PROXY_ABI = json.dumps([
    {"name": "dao", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "execute", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "address", "name": "_target"}, {"type": "bytes", "name": "_calldata"}], "outputs": []},
])

ERC20_ABI = json.dumps([
    {"name": "approve", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "address", "name": "spender"}, {"type": "uint256", "name": "amount"}],
     "outputs": [{"type": "bool"}]},
    {"name": "balanceOf", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "address", "name": "owner"}], "outputs": [{"type": "uint256"}]},
    {"name": "decimals", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}]},
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
        pkey = account.Account.decrypt(json.load(f), getpass())
        return account.Account.from_key(pkey)


def revert_reason(e: Exception) -> str:
    data = e.args[0] if e.args else b''
    if isinstance(data, bytes) and data[:4] == method_id("Error(string)"):
        return abi_decode("(string)", data[4:])[0]
    return str(e)


def curve_calls(markets) -> list:
    """Pool admin calls of the Curve vote, as in the notebook: (pool, calldata)"""
    calls = []
    for m in markets:
        if m['pool'].reserved_profit_fraction() != m['cfg'].rpf:
            calls.append((m['pool'].address,
                          SET_FEE_PARAMETERS_SELECTOR + m['cfg'].rpf.to_bytes(32, 'big') + bytes(32)))
        calls.append((m['pool'].address, SET_POLICY_SELECTOR + bytes(12) + bytes.fromhex(m['cfg'].policy[2:])))
    return calls


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


def check_lt(m, zap):
    """Borrower fees are donated; a debt at price_oracle fails the LT gate; LTDepositZap deposits, LT withdraws"""
    lt, amm, pool = m['lt'], m['amm'], m['pool']
    pool_value = pool.totalSupply() * pool.lp_price() // 10**18
    donation_apy = market_rate(m['cfg']) * 365 * 86400 * amm.get_debt() // pool_value / 1e18
    # Donation shares can fall meanwhile: the donation's price_scale update spends unlocked ones
    donation_shares, pool_stables = pool.donation_shares(), pool.balances(0)
    lt.distribute_borrower_fees()
    donated = pool.balances(0) - pool_stables
    print(f"  {m['cfg'].symbol}: pool donation APY at this debt / pool value {donation_apy:.4%}, donated "
          f"{donated / 1e18:,.2f} crvUSD, donation shares {donation_shares / 1e18:,.4f} -> {pool.donation_shares() / 1e18:,.4f}")
    assert donated > 0, "Borrower fees were not donated"

    asset = boa.loads_abi(ERC20_ABI, name="Asset").at(lt.ASSET_TOKEN())
    decimals = asset.decimals()
    assets = DEPOSIT_USD * 10**(18 + decimals) // pool.price_oracle()
    boa.deal(asset, DEPOSITOR, assets, adjust_supply=False)
    with boa.env.prank(DEPOSITOR):
        asset.approve(lt.address, 2**256 - 1)
        asset.approve(zap.address, 2**256 - 1)
        try:
            with boa.env.anchor():
                lt.deposit(assets, assets * pool.price_oracle() // 10**decimals, 0)
            raise SystemExit(f"ERROR: {m['cfg'].symbol} deposit with debt at price_oracle passed: no LT gate?")
        except boa.BoaError as e:
            assert "LT deviation" in str(e), e

        preview = zap.preview_deposit(lt.address, assets)
        shares = zap.deposit(lt.address, assets, preview * 999 // 1000)
        withdrawn = lt.withdraw(shares, 0)
    # The deposit (at the book's ratio) and the withdrawal (fixed crvUSD out) each pay the pool fee on their
    # imbalance against the pool, which grows the further price_scale lags the market: at most the fee twice
    fee = pool.fee()
    print(f"    ${DEPOSIT_USD:,} of {asset.address}: debt at price_oracle fails the LT gate; LTDepositZap -> "
          f"{shares / 1e18:,.6f} shares, withdrawn {withdrawn / assets - 1:+.3%} (pool fee {fee / 1e8:.2f}%, "
          f"price_scale {pool.price_scale() / pool.price_oracle() - 1:+.2%} off price_oracle)")
    assert withdrawn * 10**10 >= assets * (10**10 - 2 * fee), "The round trip lost more than twice the pool fee"


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
    lt_deployer = boa.load_partial('contracts/LT.vy')
    amm_deployer = boa.load_partial('contracts/AMM.vy')
    pool_deployer = boa.load_partial('contracts/twocrypto_pool/contracts/main/Twocrypto.vy')

    markets = []
    for cfg in MARKETS:
        market = factory.markets(cfg.id)
        lt = lt_deployer.at(market.lt)
        amm = amm_deployer.at(market.amm)
        pool = pool_deployer.at(market.cryptopool)

        symbol = lt.symbol()
        assert symbol == cfg.symbol, f"Market {cfg.id} is {symbol}, expected {cfg.symbol}"
        assert pool.address == cfg.pool, f"Market {cfg.id} cryptopool is {pool.address}, expected {cfg.pool}"
        markets.append({'cfg': cfg, 'lt': lt, 'amm': amm, 'pool': pool})

        rate, fee = market_rate(cfg), market_fee(cfg)
        print(f"Market {cfg.id} ({symbol}), LT {lt.address}, AMM {amm.address}")
        print(f"  cryptopool:  {pool.address}")
        print(f"  POLICY():    {pool.POLICY()} -> {cfg.policy}")
        print(f"  RPF:         {pool.reserved_profit_fraction() / 1e10:.2%} -> {cfg.rpf / 1e10:.2%} (by Curve)")
        print(f"  rate:        {amm.rate()} -> {rate}  "
              f"({amm.rate() * 365 * 86400 / 1e18:.4%} -> {rate * 365 * 86400 / 1e18:.4%} per year)")
        print(f"  AMM fee:     {amm.fee() / 1e18:.4%} -> {fee / 1e18:.4%}")

    # Guard: revert unless all four underlying Curve pools already run the policy contracts which the Curve
    # ownership vote installs, i.e. unless that vote has executed.
    actions = [
        Action(to=comparator.address, value=0,
               data=comparator.check_equal.prepare_calldata(m['pool'].address, POLICY_SELECTOR,
                                                            int(m['cfg'].policy, 16)))
        for m in markets
    ]
    for m in markets:
        actions.append(Action(to=factory_owner.address, value=0,
                              data=factory_owner.lt_set_rate.prepare_calldata(m['lt'].address, market_rate(m['cfg']))))
        actions.append(Action(to=factory_owner.address, value=0,
                              data=factory_owner.lt_set_amm_fee.prepare_calldata(m['lt'].address, market_fee(m['cfg']))))

    curve_vote = (f'Curve vote {CURVE_VOTE_ID} [https://www.curve.finance/dao/ethereum/proposals/{CURVE_VOTE_ID}-ownership]'
                  if CURVE_VOTE_ID else 'The Curve ownership vote')
    title = 'Donation APY and releverage fees for the WETH and BTC markets under the new pool policies'
    summary = (
        f'{curve_vote} attaches new YBOraclizedPolicy contracts, driven by Pyth Pro reports, to the yb-WBTC, '
        'yb-cbBTC, yb-tBTC and yb-WETH pools, and sets their reserved profit fractions to 48.75%, 45%, 48.75% and '
        '43%. On YB side, we tune the markets to match. Pool donation APY: yb-WBTC and yb-tBTC 0.85%, yb-cbBTC '
        '0.55%, yb-WETH 0.90% (1.70%, 1.10% and 1.80% per year on crvUSD at 2x leverage, from 1.80% for the BTC '
        'markets and 2.60% for yb-WETH). Releverage fee: 180 bps for the BTC markets (from 120 bps) and 140 bps '
        'for yb-WETH (from 150 bps). Our vote is only executable once the Curve vote is executed.'
    )

    if not FORK:
        if not CURVE_VOTE_ID:
            print("Note: CURVE_VOTE_ID is not set, so the description does not link the Curve vote")
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
        calls = curve_calls(markets)
        if CURVE_VOTE_ID:
            curve_voting = boa.loads_abi(CURVE_VOTING_ABI, name="CurveVoting").at(CURVE_VOTING)
            curve_script = curve_voting.getVote(CURVE_VOTE_ID)[9]
            for target, calldata in calls:
                assert bytes.fromhex(target[2:]) in curve_script, f"Curve vote {CURVE_VOTE_ID} does not touch {target}"
                assert calldata in curve_script, f"Curve vote {CURVE_VOTE_ID} lacks {target}: 0x{calldata.hex()}"
            print(f"\nCurve vote {CURVE_VOTE_ID} installs the expected policies and RPFs")

        print("\n=== YB vote before Curve's: must revert ===")
        for m in markets:
            assert m['pool'].POLICY() != m['cfg'].policy, \
                f"Pool {m['pool'].address} already runs the new policy - guard is a no-op"
        with boa.env.anchor():
            try:
                with boa.env.prank(DAO):
                    for action in actions:
                        boa.env.raw_call(to_address=action.to, data=action.data)
                raise SystemExit("ERROR: YB vote executed while the previous policies are still in place!")
            except Exception as e:
                print(f"  correctly reverted: {revert_reason(e)}")

        if CURVE_VOTE_ID:
            print(f"\n=== Executing Curve ownership vote {CURVE_VOTE_ID} via Convex ===")
            execute_curve_vote(curve_voting, CURVE_VOTE_ID)
        else:
            print("\n=== Curve vote not created yet: its actions through the ownership agent ===")
            proxy = boa.loads_abi(AMM_OWNER_PROXY_ABI, name="AmmOwnerProxy").at(AMM_OWNER_PROXY)
            assert proxy.dao() == CURVE_AGENT, "The ownership agent does not own the AmmOwnerProxy"
            with boa.env.prank(CURVE_AGENT):
                for target, calldata in calls:
                    proxy.execute(target, calldata)
                    print(f"  {target}: 0x{calldata.hex()}")
        for m in markets:
            pool = m['pool']
            print(f"  pool {pool.address} POLICY() = {pool.POLICY()}, RPF = {pool.reserved_profit_fraction() / 1e10:.2%}")
            assert pool.POLICY() == m['cfg'].policy, "Curve's side did not install the expected policy"
            assert pool.reserved_profit_fraction() == m['cfg'].rpf, "Curve's side did not set the expected RPF"

        print("\n=== YB vote after Curve's ===")
        with boa.env.prank(DAO):
            for i, action in enumerate(actions):
                boa.env.raw_call(to_address=action.to, data=action.data)
                print(f"  action {i + 1} out of {len(actions)}")

        for m in markets:
            amm = m['amm']
            print(f"  market {m['cfg'].id}: rate {amm.rate()} ({amm.rate() * 365 * 86400 / 1e18:.4%} per year), "
                  f"AMM fee {amm.fee() / 1e18:.4%}")
            assert amm.rate() == market_rate(m['cfg']), "Rate not updated as expected"
            assert amm.fee() == market_fee(m['cfg']), "Fee not updated as expected"

        print("\n=== LT under the new policies ===")
        boa.env.time_travel(seconds=86400)
        zap = boa.load_partial('contracts/utils/LTDepositZap.vy').at(DEPOSIT_ZAP)
        for m in markets:
            check_lt(m, zap)
        print("OK")
