#!/usr/bin/env python3
"""
Deploy a Merkl Pull-on-Claim wrapper for crvUSD, held by MerklPIDDriver, EXACTLY the way Merkl
deploys their wrappers: a standard ERC1967Proxy over Merkl's own on-chain, audited, verified
PullTokenWrapper implementation, initialized with (crvUSD, DistributionCreator, holder, name,
symbol). Nothing custom is compiled here - the proxy bytecode is Merkl's exact on-chain
bytecode (scripts/erc1967_proxy.json), so the deployment is an Etherscan exact-match, and all
the logic is Merkl's verified implementation.

Flow the wrapper implements: the driver mints wrapper tokens to fund a campaign; at claim time
the wrapper pulls crvUSD from the holder (MerklPIDDriver) via allowance. So the reward token is
this wrapper, crvUSD stays in the reserve, and it is pulled only as users claim.

Run with FORK = True first to simulate on a mainnet fork and print everything for Merkl to
review; set FORK = False (and HOLDER to the deployed MerklPIDDriver) to broadcast from yb-deployer.

    cd scripts && python deploy_merkl_wrapper.py
"""
import os
import sys
import json
from getpass import getpass
import boa
from eth_abi import encode
from eth_utils import keccak
from eth_account import account

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from networks import NETWORK   # noqa: E402

FORK = False
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"     # YB Deployer

# --- what we deploy ----------------------------------------------------------
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
DISTRIBUTION_CREATOR = "0x8BB4C975Ff3c250e0ceEA271728547f3802B36Fd"   # Merkl DistributionCreator
# Merkl's on-chain, Etherscan-verified PullTokenWrapper implementation (mint(uint256) mints to
# the holder; the transfer hook pulls the underlying from the holder at claim). Shared by their
# wrappers - our proxy just points at it.
PULL_TOKEN_WRAPPER_IMPL = "0x979a04fd2f3a6a2b3945a715e24b974323e93567"
# Placeholder holder for this test deploy. initialize() rejects address(0), so this is a
# non-zero stand-in (the deployer); the real deploy points it at the MerklPIDDriver.
HOLDER = DEPLOYER
NAME = "Yield Basis crvUSD (Merkl wrapper)"
SYMBOL = "ybwcrvUSD"

HERE = os.path.dirname(os.path.abspath(__file__))
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

WRAPPER_ABI = json.dumps([
 {"name": "token", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "holder", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "distributor", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "name", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "string"}]},
 {"name": "symbol", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "string"}]},
 {"name": "setFeeRecipient", "stateMutability": "nonpayable", "type": "function", "inputs": [], "outputs": []},
])


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


def deploy_wrapper(holder):
    """ERC1967Proxy(PullTokenWrapper impl, initialize(crvUSD, DC, holder, name, symbol))."""
    proxy_bytecode = bytes.fromhex(json.load(open(os.path.join(HERE, "erc1967_proxy.json")))["bytecode"][2:])
    init = keccak(text="initialize(address,address,address,string,string)")[:4] + encode(
        ["address", "address", "address", "string", "string"],
        [CRVUSD, DISTRIBUTION_CREATOR, holder, NAME, SYMBOL])
    ctor = encode(["address", "bytes"], [PULL_TOKEN_WRAPPER_IMPL, init])
    addr = boa.env.deploy_code(bytecode=proxy_bytecode + ctor)
    if isinstance(addr, tuple):
        addr = addr[0]
    return boa.loads_abi(WRAPPER_ABI).at(addr), init


def _impl_slot(wrapper):
    try:
        val = boa.env.evm.get_storage(wrapper.address, int(EIP1967_IMPL_SLOT, 16))
        return "0x%040x" % (val & (2**160 - 1))
    except Exception as e:
        return f"(unread: {e})"


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK)
        boa.env.eoa = DEPLOYER                       # act as yb-deployer on the fork
    else:
        boa.set_network_env(NETWORK)
        boa.env.add_account(account_load('yb-deployer'))

    wrapper, init = deploy_wrapper(HOLDER)
    wrapper.setFeeRecipient()                        # point the fee hook at DistributionCreator.feeRecipient()

    print("\n=== Merkl crvUSD wrapper deployed ===")
    print("wrapper (ERC1967Proxy) :", wrapper.address)
    print("implementation         :", PULL_TOKEN_WRAPPER_IMPL, "(Merkl's verified PullTokenWrapper)")
    print("EIP-1967 impl slot     :", _impl_slot(wrapper))
    print("token()                :", wrapper.token(), "(crvUSD)")
    print("holder()               :", wrapper.holder(), "(placeholder for this test; real = MerklPIDDriver)")
    print("distributor()          :", wrapper.distributor())
    print("name / symbol          :", repr(wrapper.name()), "/", repr(wrapper.symbol()))
    print("initialize calldata    : 0x" + init.hex())
    print("\nNext: ask Merkl to whitelist this wrapper, then call set_merkl(DistributionCreator, wrapper)")
    print("on MerklPIDDriver (which grants crvUSD->wrapper and wrapper->DistributionCreator allowances).")
