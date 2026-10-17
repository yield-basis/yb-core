"""
ENS records for the per-market oracle subtrees, shared by deploy_lending_oracle.py
(oracles.yieldbasis.eth) and deploy_lending_oracle_ll.py (oracles-ll.yieldbasis.eth):

    <parent>               -> the root contract (oracle singleton / LL factory)
    <label>.<parent>       -> the market's asset oracle
    usd.<label>.<parent>   -> the market's USD oracle

All names are wrapped and normally owned by ENS_OWNER (a YB multisig).

ENS record auth is per-node and doesn't inherit down the tree, so writing every record needs
authority on every node. Rather than N approvals, this uses ONE scoped multisig action: the
owner transfers the wrapped parent name to the deployer. The deployer then owns the parent, so it
can re-own each child (setSubnodeRecord both creates a missing child AND takes over an existing
one), setAddr it, and hand each child back to ENS_OWNER - then transfer the parent itself back.
So the multisig does exactly one thing (transfer in); the deployer returns everything. Trade-off
vs an approval: the deployer holds full ownership of the parent for the duration of the run
(scoped to that name only), so it relies on the deployer key for that window. In FORK mode the
transfer in/out is simulated.
"""
import json
import boa
from eth_utils import keccak

ENS_LABELS = {7: "wbtc", 8: "cbbtc", 9: "tbtc", 10: "weth"}
ENS_OWNER = "0xC1671c9efc9A2ecC347238BeA054Fc6d1c6c28F9"   # wrapped-name owner; subnames end up here
NAME_WRAPPER = "0xD4416b13d2b3a9aBae7AcD5D6C2BbDBE25686401"
PUBLIC_RESOLVER = "0x231b0Ee14048e9dCcD1d247744d114a4EB5E8E63"

NAME_WRAPPER_ABI = json.dumps([
    {"name": "setSubnodeRecord", "stateMutability": "nonpayable", "type": "function", "inputs": [
        {"type": "bytes32"}, {"type": "string"}, {"type": "address"}, {"type": "address"},
        {"type": "uint64"}, {"type": "uint32"}, {"type": "uint64"}], "outputs": [{"type": "bytes32"}]},
    {"name": "safeTransferFrom", "stateMutability": "nonpayable", "type": "function", "inputs": [
        {"type": "address"}, {"type": "address"}, {"type": "uint256"}, {"type": "uint256"},
        {"type": "bytes"}], "outputs": []},
    {"name": "ownerOf", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]},
    {"name": "getData", "stateMutability": "view", "type": "function", "inputs": [{"type": "uint256"}],
     "outputs": [{"type": "address"}, {"type": "uint32"}, {"type": "uint64"}]},
])
RESOLVER_ABI = json.dumps([
    {"name": "setAddr", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "bytes32"}, {"type": "address"}], "outputs": []},
    {"name": "addr", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "bytes32"}], "outputs": [{"type": "address"}]},
])


def namehash(name):
    node = b"\x00" * 32
    for label in reversed(name.split(".")):
        node = keccak(node + keccak(text=label))
    return node


def _tokid(node):
    return int.from_bytes(node, "big")


def transfer_in_call(parent_name, deployer):
    """The one multisig action that has to precede a broadcast run."""
    return (f"NameWrapper({NAME_WRAPPER}).safeTransferFrom({ENS_OWNER}, {deployer}, "
            f"{_tokid(namehash(parent_name))}, 1, 0x)  (one call, from {ENS_OWNER})")


def require_owner(parent_name):
    """Broadcast preflight: abort BEFORE deploying anything unless the deployer already owns the
    wrapped parent (otherwise the ENS step would only fail after the whole deployment)."""
    nw = boa.loads_abi(NAME_WRAPPER_ABI).at(NAME_WRAPPER)
    me = str(boa.env.eoa)
    if nw.ownerOf(_tokid(namehash(parent_name))) != me:
        raise SystemExit(f"deployer must own {parent_name} first (one multisig action):\n  "
                         f"{transfer_in_call(parent_name, me)}")


def _set_subtree(parent_name, root_addr, oracles):
    nw = boa.loads_abi(NAME_WRAPPER_ABI).at(NAME_WRAPPER)
    res = boa.loads_abi(RESOLVER_ABI).at(PUBLIC_RESOLVER)
    parent = namehash(parent_name)
    me = str(boa.env.eoa)
    exp = nw.getData(_tokid(parent))[2]                     # mirror the parent's expiry on children
    assert nw.ownerOf(_tokid(parent)) == me, f"deployer must own {parent_name} (multisig transfer first)"

    print(f"\n--- ENS: rewriting {parent_name} subtree ---")
    res.setAddr(parent, root_addr)
    print(f"    ens {parent_name} -> {root_addr}")
    for mid, usd, asset in oracles:
        label = ENS_LABELS[mid]
        anode = keccak(parent + keccak(text=label))
        unode = keccak(anode + keccak(text="usd"))
        nw.setSubnodeRecord(parent, label, me, PUBLIC_RESOLVER, 0, 0, exp)    # own <label>.<parent>
        res.setAddr(anode, asset)
        nw.setSubnodeRecord(anode, "usd", me, PUBLIC_RESOLVER, 0, 0, exp)     # own usd.<label>.<parent>
        res.setAddr(unode, usd)
        nw.setSubnodeRecord(anode, "usd", ENS_OWNER, PUBLIC_RESOLVER, 0, 0, exp)   # usd -> owner
        nw.setSubnodeRecord(parent, label, ENS_OWNER, PUBLIC_RESOLVER, 0, 0, exp)  # <label> -> owner
        print(f"    ens {label}.<parent> -> {asset}   usd.{label}.<parent> -> {usd}")
    nw.safeTransferFrom(me, ENS_OWNER, _tokid(parent), 1, b"")               # return the parent


def write_subtree(parent_name, root_addr, oracles, fork):
    """(Re)write the <parent_name> subtree. oracles: [(market_id, usd_oracle, asset_oracle)].
    FORK: simulate the multisig transfer-in, then check every record and that ENS_OWNER owns
    every name again. Broadcast: the deployer must already own the parent (see require_owner)."""
    nw = boa.loads_abi(NAME_WRAPPER_ABI).at(NAME_WRAPPER)
    pid = _tokid(namehash(parent_name))
    if fork:
        # Simulate the one multisig transfer-in (a no-op if the deployer already holds it).
        cur, me = nw.ownerOf(pid), str(boa.env.eoa)
        if cur != me:
            with boa.env.prank(cur):
                nw.safeTransferFrom(cur, me, pid, 1, b"")
    _set_subtree(parent_name, root_addr, oracles)
    if fork:
        res = boa.loads_abi(RESOLVER_ABI).at(PUBLIC_RESOLVER)
        expected = [(parent_name, root_addr)]
        for mid, usd, asset in oracles:
            expected += [(f"{ENS_LABELS[mid]}.{parent_name}", asset), (f"usd.{ENS_LABELS[mid]}.{parent_name}", usd)]
        for name, addr in expected:
            node = namehash(name)
            assert res.addr(node) == addr, f"ens {name} -> {res.addr(node)}, expected {addr}"
            assert nw.ownerOf(_tokid(node)) == ENS_OWNER, f"ens {name} not returned to {ENS_OWNER}"
        print(f"ENS {parent_name} subtree rewritten + returned (fork-simulated transfer) - OK")
    else:
        print(f"\nENS subtree written; {parent_name} returned to {ENS_OWNER}.")
