#!/usr/bin/env python3
"""
Warm-up swaps on the new YB v3 markets (IDs 7-10) so arb bots start watching
both the new Curve cryptopools and the YB VirtualPools. For each market, swap
a small amount of the collateral asset to crvUSD and back, once through the
cryptopool and once through the VirtualPool.

Usage:
  python scripts/warmup_yb_v3_swaps.py          # production, signed by yb-deployer
  python scripts/warmup_yb_v3_swaps.py --test   # forked dry-run via boa.fork
"""
import json
import os
import sys
import warnings

warnings.filterwarnings(
    "ignore",
    message="casted bytecode does not match compiled bytecode.*",
    category=UserWarning,
)

import boa

from networks import NETWORK

WARMUP_ACCOUNT = "yb-deployer"
YB_FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"

MARKET_IDS = [7, 8, 9, 10]

# Per-collateral warm-up swap size in human units. Each market does TWO
# roundtrips (cryptopool + VirtualPool), so the asset balance must cover at
# least one swap_amount — the rest is returned (minus fees) on the way back.
SWAP_AMOUNTS = {
    "WBTC":  0.00005,
    "cbBTC": 0.00005,
    "tBTC":  0.00005,
    "WETH":  0.005,
}

ERC20_ABI_PATH = os.path.join(os.path.dirname(__file__), "erc20.abi.json")

# An allowance >= this is treated as "effectively infinite" and not re-set.
APPROVAL_THRESHOLD = 2**128

# Fixed gas limit for swap txs. boa's gas estimation has been falling short on
# the VirtualPool flash-loan path (OOG on send), so we pin it well above the
# observed worst-case instead of estimating per tx.
SWAP_GAS = 2_000_000


def _load_erc20(addr: str):
    return boa.load_abi(ERC20_ABI_PATH, name="ERC20").at(addr)


def _keystore_address(name: str) -> str:
    path = os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", name + ".json"))
    with open(path) as f:
        return "0x" + json.load(f)["address"]


def _account_load(fname: str):
    from eth_account import account
    from getpass import getpass
    key_path = os.path.expanduser(
        os.path.join("~", ".brownie", "accounts", fname + ".json"))
    with open(key_path) as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
    return account.Account.from_key(pkey)


def ensure_infinite_approval(token, spender: str):
    """Set an unlimited allowance from the current EOA on `token` for
    `spender`, but skip the tx if the existing allowance is already
    effectively infinite (>= APPROVAL_THRESHOLD)."""
    eoa = boa.env.eoa
    current = token.allowance(eoa, spender)
    if current >= APPROVAL_THRESHOLD:
        return
    print(f"  approving {token.symbol()} on {spender} (was {current})")
    token.approve(spender, 2**256 - 1)


def roundtrip(pool, asset, crvusd, swap_raw: int, label: str) -> int:
    """Swap `swap_raw` of `asset` -> crvUSD -> asset on `pool`. Returns the
    raw asset cost (swap_raw minus what came back). Uses the actual crvUSD
    received from the forward swap as the input to the reverse swap, so the
    only standing balance the EOA needs is `swap_raw` of `asset`. Assumes
    allowances are already in place — see ensure_infinite_approval."""
    eoa = boa.env.eoa
    asset_decimals = asset.decimals()
    asset_symbol = asset.symbol()
    asset_before = asset.balanceOf(eoa)
    crvusd_before = crvusd.balanceOf(eoa)

    pool.exchange(1, 0, swap_raw, 0, gas=SWAP_GAS)
    crvusd_received = crvusd.balanceOf(eoa) - crvusd_before
    print(f"  [{label}] {swap_raw / 10**asset_decimals:.10f} {asset_symbol} "
          f"-> {crvusd_received / 1e18:.6f} crvUSD")

    pool.exchange(0, 1, crvusd_received, 0, gas=SWAP_GAS)
    asset_returned = asset.balanceOf(eoa) - (asset_before - swap_raw)
    cost = swap_raw - asset_returned
    print(f"  [{label}] {crvusd_received / 1e18:.6f} crvUSD -> "
          f"{asset_returned / 10**asset_decimals:.10f} {asset_symbol} "
          f"(roundtrip cost {cost / 10**asset_decimals:.10f} {asset_symbol})")
    return cost


def main():
    test_mode = "--test" in sys.argv[1:]

    if test_mode:
        boa.fork(NETWORK, block_identifier="latest")
        boa.env.eoa = _keystore_address(WARMUP_ACCOUNT)
    else:
        boa.set_network_env(NETWORK)
        boa.env.add_account(
            _account_load(WARMUP_ACCOUNT), force_eoa=True)

    print(f"Warm-up account: {boa.env.eoa}"
          + (" (fork)" if test_mode else ""))

    yb_factory = boa.load_partial("contracts/Factory.vy").at(YB_FACTORY)
    pool_interface = boa.load_partial(
        "contracts/twocrypto_pool/contracts/main/Twocrypto.vy")
    virtual_pool_interface = boa.load_partial("contracts/VirtualPool.vy")
    crvusd = _load_erc20(CRVUSD)

    # Resolve pool/asset addresses once and pre-approve everything we'll need
    # (asset + crvUSD on both pools per market). Approvals are idempotent —
    # ensure_infinite_approval no-ops if the allowance is already infinite —
    # so re-running this script doesn't send extra approve() txs.
    plan = []
    for mid in MARKET_IDS:
        market = yb_factory.markets(mid)
        asset = _load_erc20(market.asset_token)
        if asset.symbol() not in SWAP_AMOUNTS:
            print(f"\n=== Market #{mid} ({asset.symbol()}) ===")
            print(f"  SKIP: no SWAP_AMOUNTS entry for {asset.symbol()}")
            continue
        plan.append({
            "mid": mid,
            "asset": asset,
            "cryptopool": pool_interface.at(market.cryptopool),
            "virtual_pool": virtual_pool_interface.at(market.virtual_pool),
        })

    print("\n=== Ensuring infinite approvals ===")
    for p in plan:
        for pool in (p["cryptopool"], p["virtual_pool"]):
            ensure_infinite_approval(p["asset"], pool.address)
            ensure_infinite_approval(crvusd, pool.address)

    for p in plan:
        mid = p["mid"]
        asset = p["asset"]
        cryptopool = p["cryptopool"]
        virtual_pool = p["virtual_pool"]
        symbol = asset.symbol()
        decimals = asset.decimals()
        swap_human = SWAP_AMOUNTS[symbol]
        swap_raw = int(round(swap_human * 10**decimals))

        print(f"\n=== Market #{mid} ({symbol}) ===")
        print(f"  cryptopool:   {cryptopool.address}")
        print(f"  virtual_pool: {virtual_pool.address}")
        balance = asset.balanceOf(boa.env.eoa)
        print(f"  {symbol} balance: {balance / 10**decimals:.10f}, "
              f"swap size: {swap_human}")
        if balance < swap_raw:
            print(f"  SKIP: balance < {swap_human} {symbol}")
            continue

        roundtrip(cryptopool, asset, crvusd, swap_raw, "cryptopool")
        roundtrip(virtual_pool, asset, crvusd, swap_raw, "VirtualPool")

    print("\n=== Done ===")


if __name__ == "__main__":
    main()
