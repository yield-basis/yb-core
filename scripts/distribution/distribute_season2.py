#!/usr/bin/env python3
"""
Season 2 YB distribution: sends YB from the ADMIN EOA to every wallet of the
allocation CSV through a new Multisend.

The season 1 Multisend can't be reused. Its already_sent is permanent, so it
would silently skip every wallet that received YB in season 1.

The CSV next to this script is a copy of yb_allocation_v3cap.csv from
yb-research-scripts/pnl (written by scripts/yb_allocation.py) at end block
26343097. Only the `user` and `yb` columns are used.

From the repo root:
    # 1. rehearse the whole distribution on a fork, nothing is sent
    uv run python scripts/distribution/distribute_season2.py --fork [--rpc URL]

    # 2. deploy the Multisend once, then set MULTISEND below
    uv run python scripts/distribution/distribute_season2.py --deploy

    # 3. fund the ADMIN EOA, then approve and send
    uv run python scripts/distribution/distribute_season2.py

Step 3 is safe to re-run after a failure: it only pays wallets that the
Multisend has not marked as sent. That protection lives in the contract, which
is why step 3 refuses to run without a fixed MULTISEND: a second deployment
would start with a clean state and pay everyone again.
"""

import argparse
import csv
import hashlib
import json
import os
from decimal import Decimal
from getpass import getpass
from time import sleep
import boa
from eth_account import account
from eth_utils import to_checksum_address


YB = "0x01791F726B4103694969820be083196cC7c045fF"
DEPLOYER = "0xa41074e0472E4e014c655dD143E9f5b87784a9DF"
SEASON_1_MULTISEND = "0x0926a73Bb2D169a86c2Da9c7E03aC63A5cf42883"
YB_HOLDER = "0x38667AEC4a9793A91e7986c43Fba502BDE89D86C"  # funds the ADMIN on a fork only

MULTISEND = None  # set after --deploy
CSV_SHA256 = "da202357bb86b16294349827903ee07a48bdabb61e9b0590d62770c54fec0728"

CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yb_allocation_v3cap.csv")
TOTAL = 1_406_250 * 10**18
MAX_ROUNDING = 10**12  # 1e-6 YB: the CSV holds floats, so its sum is only close to TOTAL
BATCH_SIZE = 100
LIVE_PAUSE = 60  # seconds between transactions, for lagging RPCs


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.Account.decrypt(json.load(f), getpass())
        return account.Account.from_key(pkey)


def load_allocation(path, total=TOTAL):
    """{checksummed wallet: amount in wei}, adding up to exactly `total`."""
    amounts = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            user = to_checksum_address(row["user"])
            assert user not in amounts, f"Duplicate wallet {user}"
            amounts[user] = int(Decimal(row["yb"]) * 10**18)
    rounding = total - sum(amounts.values())
    assert abs(rounding) <= MAX_ROUNDING, f"CSV adds up to {sum(amounts.values())}, expected {total}"
    amounts[max(amounts, key=amounts.get)] += rounding
    assert min(amounts.values()) > 0, "Wallet with nothing to receive"
    return amounts


def distribute(multisend, yb, admin, amounts, pause=0):
    """Pays every wallet not marked as sent yet. Returns how much it sent."""
    assert admin not in amounts, "The sender can't be a recipient"
    pending = [(user, amount) for user, amount in amounts.items() if not multisend.already_sent(user)]
    remaining = sum(amount for _, amount in pending)
    print(f"Wallets left: {len(pending)} of {len(amounts)}, YB left: {remaining / 1e18:,.6f}")
    if not pending:
        return 0

    balance = yb.balanceOf(admin)
    assert balance >= remaining, f"ADMIN holds {balance / 1e18:,.6f} YB, needs {remaining / 1e18:,.6f}"
    if yb.allowance(admin, multisend.address) < remaining:
        yb.approve(multisend.address, remaining)
        sleep(pause)

    for start in range(0, len(pending), BATCH_SIZE):
        users, batch_amounts = zip(*pending[start:start + BATCH_SIZE])
        before = yb.balanceOf(admin)
        multisend.send(list(users), list(batch_amounts))
        sent = before - yb.balanceOf(admin)
        print(f"Batch from {start}: {len(users)} wallets, sent {sent / 1e18:,.6f} YB")
        assert sent == sum(batch_amounts), f"Sent {sent}, expected {sum(batch_amounts)}: stopping, re-run to continue"
        sleep(pause)
    return remaining


def check_multisend(multisend, admin):
    assert multisend.address != SEASON_1_MULTISEND, "Season 1 Multisend would skip its past recipients"
    assert multisend.ADMIN() == admin, f"Multisend belongs to {multisend.ADMIN()}"
    assert multisend.TOKEN() == YB, f"Multisend sends {multisend.TOKEN()}"


def rehearse(amounts):
    """The whole distribution on a fork, checked wallet by wallet."""
    admin = DEPLOYER
    boa.env.eoa = admin
    yb = boa.load_partial('contracts/dao/YB.vy').at(YB)
    if MULTISEND:
        multisend = boa.load_partial('contracts/dao/Multisend.vy').at(MULTISEND)
    else:
        multisend = boa.load('contracts/dao/Multisend.vy', YB)
    check_multisend(multisend, admin)

    with boa.env.prank(YB_HOLDER):
        yb.transfer(admin, TOTAL)
    before = {user: yb.balanceOf(user) for user in amounts}
    admin_before = yb.balanceOf(admin)

    assert distribute(multisend, yb, admin, amounts) == TOTAL
    assert distribute(multisend, yb, admin, amounts) == 0, "A second run must send nothing"

    wrong = [user for user, amount in amounts.items() if yb.balanceOf(user) - before[user] != amount]
    assert not wrong, f"{len(wrong)} wallets got the wrong amount, e.g. {wrong[0]}"
    assert admin_before - yb.balanceOf(admin) == TOTAL
    assert yb.allowance(admin, multisend.address) == 0
    print(f"Fork check passed: {len(amounts)} wallets got exactly their amount, {TOTAL / 1e18:,.0f} YB in total")


def send_live(amounts, digest):
    assert MULTISEND, "Set MULTISEND first (--deploy)"
    assert digest == CSV_SHA256, f"CSV is not the reviewed one: sha256 {digest}"
    admin = account_load('ADMIN')
    assert admin.address == DEPLOYER, f"Loaded {admin.address}"
    boa.env.add_account(admin)
    yb = boa.load_partial('contracts/dao/YB.vy').at(YB)
    multisend = boa.load_partial('contracts/dao/Multisend.vy').at(MULTISEND)
    check_multisend(multisend, admin.address)

    print(f"Sending from {admin.address} through {MULTISEND}, ADMIN holds {yb.balanceOf(admin.address) / 1e18:,.6f} YB")
    if input("Type 'send' to go ahead: ") != "send":
        raise SystemExit("Nothing sent")
    distribute(multisend, yb, admin.address, amounts, pause=LIVE_PAUSE)
    print(f"Done, ADMIN holds {yb.balanceOf(admin.address) / 1e18:,.6f} YB")


def main():
    from networks import NETWORK  # untracked, next to this script

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fork", action="store_true", help="rehearse on a fork of the network, nothing is sent")
    parser.add_argument("--deploy", action="store_true", help="deploy the Multisend and stop")
    parser.add_argument("--rpc", default=NETWORK, help="RPC URL, networks.py by default")
    parser.add_argument("--csv", default=CSV, help="allocation CSV")
    args = parser.parse_args()

    amounts = load_allocation(args.csv)
    with open(args.csv, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    print(f"{args.csv}\nsha256 {digest}")
    print(f"{sum(amounts.values()) / 1e18:,.6f} YB to {len(amounts)} wallets")

    if args.fork:
        boa.fork(args.rpc)
        rehearse(amounts)
        return

    boa.set_network_env(args.rpc)
    if args.deploy:
        assert not MULTISEND, f"Already deployed at {MULTISEND}"
        admin = account_load('ADMIN')
        assert admin.address == DEPLOYER, f"Loaded {admin.address}"
        boa.env.add_account(admin)
        multisend = boa.load('contracts/dao/Multisend.vy', YB)
        print(f"Deployed. Set in this script:\nMULTISEND = \"{multisend.address}\"")
        return

    send_live(amounts, digest)


if __name__ == '__main__':
    main()
