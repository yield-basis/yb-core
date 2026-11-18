#!/usr/bin/env python3
"""
Realize LT admin fees and push them through FeeSplitter -> (MerklPIDDriver, FeeDistributor) in one tx.

The Factory's fee_receiver is the FeeSplitter, and its fill_epochs() (alias of trigger()) already does the
whole job in a single call:
  1. withdraw_admin_fees() on every LT in the FeeDistributor's current token set (best effort, failures
     swallowed) - mints the fee LT shares to the splitter
  2. sends split_fraction of each LT balance it holds to the MerklPIDDriver, the rest to the FeeDistributor
  3. driver.trigger(): driver-held LT -> asset -> crvUSD, growing the Merkl campaign reserve
  4. fd.fill_epochs(): spreads newly arrived LT over the next 4 weekly epochs for veYB holders
So no multicall is needed: pre-claiming the pools that have fees in a Multicall3 bundle ends in the identical
state and only costs extra gas, because the splitter re-runs withdraw_admin_fees() on every LT anyway.

This script forks the chain head and decides whether a trigger is worth sending. It is if any of these
hold: an LT has pending admin fees, LT shares sit in the splitter (someone called withdraw_admin_fees()
directly), the driver holds LT at or above its dust floor, or the FeeDistributor holds LT it hasn't put into
epochs yet. It then simulates the trigger and reports what goes where. With --send it broadcasts the call
after a confirmation prompt.

The gas limit is floored rather than estimated. The splitter's withdraw_admin_fees() calls swallow failures,
so eth_estimateGas can return a limit at which a claim silently runs out of gas while the tx still succeeds.

    uv run python scripts/claim_fees.py                       # dry run at head
    uv run python scripts/claim_fees.py --send                # + broadcast from ~/.brownie/accounts/yb-deployer.json
    uv run python scripts/claim_fees.py --send --account worker --min-usd 50
"""
import argparse
import json
import os
import sys
from getpass import getpass

import boa
from boa.rpc import EthereumRPC
from eth_account import account
from vyper.utils import method_id

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from networks import NETWORK  # noqa: E402

CFG = json.load(open(os.path.join(HERE, 'merkl_pid_deployment.json')))
MAX_TOKENS = 100     # FeeDistributor.MAX_TOKENS
GAS_FLOOR = 2_500_000
GAS_MULT = 1.5
INTRINSIC_GAS = 21_000 + 4 * 16   # base tx + 4 non-zero calldata bytes (selector only)

ERC20_ABI = json.dumps([
    {"name": "balanceOf", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "symbol", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "string"}]},
    {"name": "decimals", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}]},
])
POOL_ABI = json.dumps([
    {"name": "price_oracle", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
])


def cpath(p):
    return os.path.join(REPO, p)


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.Account.decrypt(json.load(f), getpass(f"password for {fname}: "))
        return account.Account.from_key(pkey)


def short_err(e):
    """Revert reason of a boa call (str(BoaError) starts with the call tree, not the reason)."""
    frame = getattr(getattr(e, "stack_trace", None), "last_frame", None)
    reason = str(getattr(frame, "pretty_vm_reason", None) or e)
    lines = [x.strip() for x in reason.splitlines() if x.strip() and not x.startswith("=")]
    return lines[0][:40] if lines else type(e).__name__


class Deployers:
    """Compiled contract deployers. Compiled once; `.at()` binds to whatever env is active at call time."""
    def __init__(self):
        self.factory = boa.load_partial(cpath('contracts/Factory.vy'))
        self.fs = boa.load_partial(cpath('contracts/net_pressure/FeeSplitter.vy'))
        self.fd = boa.load_partial(cpath('contracts/dao/FeeDistributor.vy'))
        self.driver = boa.load_partial(cpath('contracts/net_pressure/MerklPIDDriver.vy'))
        self.lt = boa.load_partial(cpath('contracts/LT.vy'))
        self.erc20 = boa.loads_abi(ERC20_ABI, name="ERC20")
        self.pool = boa.loads_abi(POOL_ABI, name="CryptoPool")


class System:
    """The live fee path, resolved from the Factory: fee_receiver (FeeSplitter) -> pid (driver) + fee_distributor."""
    def __init__(self, d: Deployers):
        self.factory = d.factory.at(CFG["factory"])
        fee_receiver = self.factory.fee_receiver()
        if fee_receiver.lower() != CFG["fee_splitter"].lower():
            sys.exit(f"Factory.fee_receiver is {fee_receiver}, not the FeeSplitter {CFG['fee_splitter']} - aborting")
        self.fs = d.fs.at(fee_receiver)
        self.fd = d.fd.at(self.fs.fee_distributor())
        pid = self.fs.pid()
        self.driver = d.driver.at(pid) if int(pid, 16) != 0 else None
        self.crvusd = d.erc20.at(CFG["crvusd"])
        self.split = self.fs.split_fraction()

        set_id = self.fd.current_token_set()
        self.token_set = []
        for i in range(MAX_TOKENS):
            try:
                self.token_set.append(self.fd.token_sets(set_id, i))
            except Exception:
                break  # past the end of the DynArray

        # LT -> market info, for every Factory market (also the ones outside the FD token set)
        self.markets = {}
        for i in range(self.factory.market_count()):
            m = self.factory.markets(i)
            asset = d.erc20.at(m[0])
            self.markets[m[3].lower()] = dict(id=i, lt=d.lt.at(m[3]), pool=d.pool.at(m[1]), asset=asset, sym=asset.symbol())

    def market(self, lt_addr):
        return self.markets[lt_addr.lower()]

    def crvusd_value(self, lt_addr, shares):
        """crvUSD value of LT shares: pricePerShare is in asset units (18-dec), times the pool's crvUSD/asset EMA."""
        if shares == 0:
            return 0
        m = self.market(lt_addr)
        return shares * m["lt"].pricePerShare() // 10**18 * m["pool"].price_oracle() // 10**18

    def fd_unfilled(self, lt_addr):
        """LT the FD holds above what it has already put into epochs (fill_epochs would distribute it)."""
        lt = self.market(lt_addr)["lt"]
        return max(lt.balanceOf(self.fd.address) - self.fd.token_balances(lt_addr), 0)

    def pending_admin_fees(self, lt_addr):
        """Shares withdraw_admin_fees() would mint to the splitter right now (simulated in an anchor)."""
        lt = self.market(lt_addr)["lt"]
        with boa.env.anchor():
            before = lt.balanceOf(self.fs.address)
            try:
                lt.withdraw_admin_fees()
            except Exception as e:
                return 0, short_err(e)
            return lt.balanceOf(self.fs.address) - before, ""

    def snapshot(self):
        s = {"reserve": self.crvusd.balanceOf(self.driver.address) if self.driver else 0}
        for t in self.token_set:
            m = self.market(t)
            s[t] = dict(
                fs=m["lt"].balanceOf(self.fs.address),
                fd_tracked=self.fd.token_balances(t),
                drv_lt=m["lt"].balanceOf(self.driver.address) if self.driver else 0,
                drv_asset=m["asset"].balanceOf(self.driver.address) if self.driver else 0,
            )
        return s


def fmt_usd(x):
    return f"${x / 1e18:,.2f}"


def inspect(sysm: System, min_usd: float):
    """Print the per-LT state; return (work_value_crvusd, necessary)."""
    fs, fd, drv = sysm.fs, sysm.fd, sysm.driver
    print(f"FeeSplitter      {fs.address}  split_fraction {sysm.split / 1e16:.1f}% -> driver")
    if drv:
        print(f"MerklPIDDriver   {drv.address}  reserve {fmt_usd(sysm.crvusd.balanceOf(drv.address))} crvUSD, "
              f"dust_floor {drv.dust_floor()} wei")
        if drv.fee_distributor().lower() != fd.address.lower():
            print(f"  WARNING: driver reads its token set from {drv.fee_distributor()}, splitter uses {fd.address}")
    else:
        print("MerklPIDDriver   none (splitter pid unset - 100% of fees go to the FeeDistributor)")
    print(f"FeeDistributor   {fd.address}  token set #{fd.current_token_set()} ({len(sysm.token_set)} LTs), "
          f"killed={fd.is_killed()}")
    if fd.is_killed():
        sys.exit("FeeDistributor is killed: its fill_epochs (and so the splitter trigger) reverts - nothing to do")

    dust = drv.dust_floor() if drv else 0
    print(f"\n{'mkt':>3} {'asset':7} {'pending fee':>22} {'in splitter':>22} {'in driver':>22} {'FD unfilled':>22}")
    total = 0
    for t in sysm.token_set:
        m = sysm.market(t)
        pending, err = sysm.pending_admin_fees(t)
        in_fs = m["lt"].balanceOf(fs.address)
        in_drv = m["lt"].balanceOf(drv.address) if drv else 0
        unfilled = sysm.fd_unfilled(t)
        work = pending + in_fs + (in_drv if in_drv >= dust else 0) + unfilled
        total += sysm.crvusd_value(t, work)

        def cell(x):
            return f"{x / 1e18:.6f} ({fmt_usd(sysm.crvusd_value(t, x))})" if x else "-"
        drv_cell = cell(in_drv) + (" <dust" if 0 < in_drv < dust else "")
        print(f"{m['id']:>3} {m['sym']:7} {(err or cell(pending)):>22} {cell(in_fs):>22} {drv_cell:>22} {cell(unfilled):>22}")

    # Markets outside the FD token set: the splitter neither claims nor forwards them, so just flag fees there
    for lt_addr, m in sysm.markets.items():
        if lt_addr in {t.lower() for t in sysm.token_set}:
            continue
        pending, _ = sysm.pending_admin_fees(lt_addr)
        if pending > 0:
            print(f"  note: market {m['id']} ({m['sym']}) has {pending / 1e18:.6f} LT ({fmt_usd(sysm.crvusd_value(lt_addr, pending))}) "
                  f"pending admin fees but is not in the FD token set - the splitter won't claim or forward it")

    necessary = total > 0 and total >= min_usd * 10**18
    print(f"\nwork for the trigger: {fmt_usd(total)} crvUSD-equivalent (threshold ${min_usd:,.2f}) -> "
          f"{'trigger' if necessary else 'nothing worth doing'}")
    return total, necessary


def simulate(sysm: System, sender, gas_price):
    """Run fs.fill_epochs() in the fork from `sender`; print the outcome; return the gas limit to send with."""
    before = sysm.snapshot()
    boa.env.evm.vm.state._account_db._reset_access_counters()   # cold-tx gas; must be outside the anchor
    with boa.env.anchor():
        with boa.env.prank(sender):
            sysm.fs.fill_epochs()
        gas = sysm.fs._computation.get_gas_used() + INTRINSIC_GAS
        after = sysm.snapshot()

    print("\n=== simulated FeeSplitter.fill_epochs() ===")
    filled_total = 0
    for t in sysm.token_set:
        m, b, a = sysm.market(t), before[t], after[t]
        filled = a["fd_tracked"] - b["fd_tracked"]
        filled_total += sysm.crvusd_value(t, filled)
        notes = []
        if a["fs"]:
            notes.append(f"{a['fs'] / 1e18:.6f} LT left in splitter")
        if a["drv_lt"]:
            notes.append(f"driver keeps {a['drv_lt'] / 1e18:.6f} LT (below dust floor)")
        if a["drv_asset"] > b["drv_asset"]:
            notes.append(f"driver swap skipped: {(a['drv_asset'] - b['drv_asset'])} {m['sym']} wei left unconverted")
        if filled or notes:
            print(f"  {m['id']:>2} {m['sym']:7} -> FD epochs {filled / 1e18:.6f} LT ({fmt_usd(sysm.crvusd_value(t, filled))}, "
                  f"1/4 per week)" + ("; " + "; ".join(notes) if notes else ""))
    gained = after["reserve"] - before["reserve"]
    print(f"  FeeDistributor: {fmt_usd(filled_total)} into the next 4 epochs")
    if sysm.driver:
        print(f"  MerklPIDDriver: +{fmt_usd(gained)} crvUSD -> reserve {fmt_usd(after['reserve'])}")

    gas_limit = max(int(gas * GAS_MULT), GAS_FLOOR)
    print(f"  gas used ~{gas:,} -> limit {gas_limit:,}; cost ~{gas * gas_price / 1e18:.5f} ETH at {gas_price / 1e9:.2f} gwei")
    return gas_limit


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--send", action="store_true", help="broadcast after the dry run (asks for confirmation)")
    ap.add_argument("--account", default="yb-deployer", help="~/.brownie/accounts/<name>.json to send from")
    ap.add_argument("--min-usd", type=float, default=0.0,
                    help="skip the trigger unless the LT it would move is worth at least this many crvUSD")
    ap.add_argument("--rpc", default=NETWORK, help="mainnet RPC (default: scripts/networks.py NETWORK)")
    args = ap.parse_args()

    acct = account_load(args.account) if args.send else None
    rpc = EthereumRPC(args.rpc)
    gas_price = int(rpc.fetch("eth_gasPrice", []), 16)

    d = Deployers()
    boa.fork(args.rpc, block_identifier="latest")
    print(f"block {boa.env.evm.patch.block_number}\n")
    sysm = System(d)

    _, necessary = inspect(sysm, args.min_usd)
    if not necessary:
        return
    sender = acct.address if acct else boa.env.eoa
    gas_limit = simulate(sysm, sender, gas_price)

    if not args.send:
        print("\ndry run - pass --send to broadcast")
        return
    eth = int(rpc.fetch("eth_getBalance", [acct.address, "latest"]), 16)
    print(f"\nsender {acct.address} ({args.account}), {eth / 1e18:.4f} ETH")
    if input("Broadcast FeeSplitter.fill_epochs()? [y/N] ").strip().lower() != "y":
        print("aborted")
        return

    # boa's network env sends without checking its local simulation when an explicit gas is given, so
    # make sure the call passes on the node itself at the latest block first.
    call = {"from": acct.address, "to": sysm.fs.address, "data": "0x" + method_id("fill_epochs()").hex(),
            "gas": hex(gas_limit)}
    rpc.fetch("eth_call", [call, "latest"])

    boa.set_network_env(args.rpc)
    boa.env.add_account(acct)
    live = System(d)
    before = live.snapshot()
    live.fs.fill_epochs(gas=gas_limit)
    after = live.snapshot()
    filled = sum(live.crvusd_value(t, after[t]["fd_tracked"] - before[t]["fd_tracked"]) for t in live.token_set)
    print(f"done: FeeDistributor +{fmt_usd(filled)} into epochs"
          + (f", driver reserve +{fmt_usd(after['reserve'] - before['reserve'])} -> {fmt_usd(after['reserve'])}"
             if live.driver else ""))


if __name__ == "__main__":
    main()
