#!/usr/bin/env python3
"""
Deploy + enable the net-pressure Merkl incentive combo, in two parts.

Part 1 (deploy): deploy YBNetPressure, MarketRateGetter, LTSwapZap, MerklPIDDriver and the
FeeSplitter, configure the driver, hand ownership to the DAO, write the addresses to JSON.

Part 2 (vote): read that JSON and build one Aragon TokenVoting proposal that, from the DAO,
installs the FeeSplitter as the Factory fee_receiver (connecting the driver) and seeds the
driver's crvUSD reserve from the deprecated markets 0-2 (recover their LT fees -> LTSwapZap ->
forward). Seed flow mirrors test_net_pressure_e2e.py::test_pid_reserve_seeded_from_deprecated_fees_via_zap.

Merkl wiring (set_merkl / create_campaign) is a later step; this only routes fees and seeds.

Run modes:
    FORK = True,  STAGE="deploy" -> deploy + vote on one fork via a temp JSON, then assert.
    FORK = True,  STAGE="vote"   -> fork at head, read DEPLOY_JSON and simulate the vote against
                                    the already-deployed contracts, then assert (no deploy).
    FORK = False, STAGE="deploy" -> broadcast part 1, verify on Etherscan, write DEPLOY_JSON.
    FORK = False, STAGE="vote"   -> read DEPLOY_JSON, size the seed on a fork, create the proposal.

    python scripts/deploy_merkl_pid_system.py
"""
import os
import json
import tempfile
from types import SimpleNamespace
from getpass import getpass
from collections import namedtuple
import boa
from boa.explorer import Etherscan
from eth_abi import encode
from eth_utils import keccak
from eth_account import account

from networks import NETWORK
from networks import PINATA_TOKEN
from networks import ETHERSCAN_API_KEY

HERE = os.path.dirname(os.path.abspath(__file__))     # .../scripts
REPO = os.path.dirname(HERE)                           # repo root (for contract paths)
VOTING = os.path.join(HERE, "voting")                  # reuse the voting ABIs + config


# --- run mode ----------------------------------------------------------------
FORK = True
STAGE = "vote"                 # non-fork only: run "deploy" then "vote"
FORK_BLOCK = 25544842  # E2E_BLOCK from test_net_pressure_e2e.py (markets 0-2 have fees); prod uses head

# --- fixed mainnet addresses -------------------------------------------------
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_UINT = 2**256 - 1
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"     # YB Deployer (impersonated as the signer in fork mode)
DAO = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"         # Aragon DAO: Factory + FeeDistributor owner; the vote executes as this
VOTING_PLUGIN = "0x2be6670DE1cCEC715bDBBa2e3A6C1A05E496ec78"  # Aragon TokenVoting
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
SINK_LP = "0x625E92624Bc2D88619ACCc1788365A69767f6200"     # crvUSD/pyUSD stableswap (Merkl's sink)
SUSDS = "0xa3931d71877C0E7a3148CB7Eb4463524FEc27fbD"       # Sky Savings Rate, for MarketRateGetter

# Merkl crvUSD Pull-on-Claim wrapper: deployed with holder = the driver, submitted to Merkl to
# whitelist, then set_merkl later. Exact-match ERC1967Proxy over Merkl's verified PullTokenWrapper
# impl - same as scripts/deploy_merkl_wrapper.py.
DISTRIBUTION_CREATOR = "0x8BB4C975Ff3c250e0ceEA271728547f3802B36Fd"   # Merkl DistributionCreator
PULL_TOKEN_WRAPPER_IMPL = "0x979a04fd2f3a6a2b3945a715e24b974323e93567"  # Merkl's verified PullTokenWrapper impl
WRAPPER_NAME = "Yield Basis crvUSD (Merkl wrapper)"
WRAPPER_SYMBOL = "ybwcrvUSD"

# --- deployment configuration (constants) ------------------------------------
MARKET_IDS = [7, 8, 9, 10]       # active markets whose net pressure the driver aggregates
DEPRECATED_MARKETS = [0, 1, 2]   # their fees seed the reserve via the vote
SPLIT_FRACTION = 15 * 10**16     # 15% of LT fees routed to the PID reserve
ZAP_SLIPPAGE = 3 * 10**18 // 2   # 1.5x slippage room for the LTSwapZap
MANAGER = DEPLOYER               # optional manager (set_gains + Merkl ops); ZERO -> DAO-only

# JSON handoff: fork uses a temp file, prod a stable file next to this script.
TMP_JSON = os.path.join(tempfile.gettempdir(), "merkl_pid_deployment.fork.json")
DEPLOY_JSON = os.path.join(HERE, "merkl_pid_deployment.json")

# Aragon TokenVoting proposal shapes (mirrors scripts/voting/create_vote.py).
Proposal = namedtuple("Proposal", ["metadata", "actions", "allowFailureMap", "startDate",
                                   "endDate", "voteOption", "tryEarlyExecution"])
Action = namedtuple("Action", ["to", "value", "data"])
# (contract, method, args): one tuple drives both the fork sim and the encoded proposal action.
Call = namedtuple("Call", ["contract", "method", "args", "label"])

ERC20_ABI = json.dumps([
 {"name": "balanceOf", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
 {"name": "decimals", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}]},
 {"name": "approve", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
 {"name": "transfer", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
])
# Factory admin is a proxy whose immutable ADMIN is the DAO; set_fee_receiver goes through it.
FACTORY_OWNER_ABI = json.dumps([
 {"name": "ADMIN", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "set_fee_receiver", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "address"}], "outputs": []},
])
WRAPPER_ABI = json.dumps([
 {"name": "token", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "holder", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "distributor", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "setFeeRecipient", "stateMutability": "nonpayable", "type": "function", "inputs": [], "outputs": []},
])


def cpath(rel):
    return os.path.join(REPO, rel)


def load_deployer():
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', 'yb-deployer.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


def pin_to_ipfs(content):
    import requests
    resp = requests.post(
        "https://api.pinata.cloud/pinning/pinJSONToIPFS",
        json={"pinataContent": content, "pinataMetadata": {"name": "merkl_pid_vote.json"},
              "pinataOptions": {"cidVersion": 1}},
        headers={"Authorization": f"Bearer {PINATA_TOKEN}", "Content-Type": "application/json"})
    assert 200 <= resp.status_code < 400, resp.text
    return 'ipfs://' + resp.json()["IpfsHash"]


# --- part 1: deploy + configure ----------------------------------------------

def _verify(contract, name, verifier):
    """Verify on Etherscan (prod only; None on a fork). Non-fatal - the contract is already deployed."""
    if verifier is None:
        return
    try:
        boa.verify(contract, verifier, wait=True)
        print(f"  verified {name} on Etherscan ({contract.address})")
    except Exception as e:
        print(f"  WARNING: Etherscan verification of {name} ({contract.address}) failed: {e}")


def deploy_wrapper(holder):
    """Deploy the Merkl crvUSD Pull-on-Claim wrapper held by `holder` (the driver): an exact-match
    ERC1967Proxy over Merkl's verified PullTokenWrapper impl (see scripts/deploy_merkl_wrapper.py)."""
    proxy_bytecode = bytes.fromhex(json.load(open(os.path.join(HERE, "erc1967_proxy.json")))["bytecode"][2:])
    init = keccak(text="initialize(address,address,address,string,string)")[:4] + encode(
        ["address", "address", "address", "string", "string"],
        [CRVUSD, DISTRIBUTION_CREATOR, holder, WRAPPER_NAME, WRAPPER_SYMBOL])
    ctor = encode(["address", "bytes"], [PULL_TOKEN_WRAPPER_IMPL, init])
    addr = boa.env.deploy_code(bytecode=proxy_bytecode + ctor)
    if isinstance(addr, tuple):
        addr = addr[0]
    return boa.loads_abi(WRAPPER_ABI).at(addr)


def deploy_stack(json_path, verifier=None):
    """Deploy the stack from the active eoa, configure the driver, hand ownership to the DAO, and
    write the addresses to `json_path`. With `verifier` (prod), verify each contract on Etherscan."""
    factory = boa.load_partial(cpath('contracts/Factory.vy')).at(FACTORY)
    fee_distributor = factory.fee_receiver()           # live FeeDistributor (fee/token-set source)
    deployer = boa.env.eoa                             # signs the deploy; transient driver owner so it can configure

    oracle = boa.load(cpath('contracts/net_pressure/YBNetPressure.vy'))
    _verify(oracle, "YBNetPressure", verifier)
    mrate = boa.load(cpath('contracts/net_pressure/MarketRateGetter.vy'), SUSDS)
    _verify(mrate, "MarketRateGetter", verifier)
    # Zap + splitter need no post-deploy config -> owned by the DAO directly.
    zap = boa.load(cpath('contracts/utils/LTSwapZap.vy'), CRVUSD, oracle.address, ZAP_SLIPPAGE, DAO)
    _verify(zap, "LTSwapZap", verifier)
    driver = boa.load(cpath('contracts/net_pressure/MerklPIDDriver.vy'),
                      CRVUSD, FACTORY, oracle.address, mrate.address, fee_distributor, deployer)
    _verify(driver, "MerklPIDDriver", verifier)
    fs = boa.load(cpath('contracts/net_pressure/FeeSplitter.vy'),
                  fee_distributor, driver.address, SPLIT_FRACTION, DAO)
    _verify(fs, "FeeSplitter", verifier)

    pressure_lts = [factory.markets(i).lt for i in MARKET_IDS]
    driver.set_pressure_lts(pressure_lts)
    driver.set_sink_pool(SINK_LP)                      # informational (Merkl measures its TVL)
    if MANAGER != ZERO_ADDRESS:
        driver.set_manager(MANAGER)
    driver.transfer_ownership(DAO)                     # gains/exec params keep ctor defaults; now DAO-owned

    # Merkl crvUSD wrapper held by the driver (submit to Merkl to whitelist; set_merkl is a later step).
    wrapper = deploy_wrapper(driver.address)
    wrapper.setFeeRecipient()                          # point the fee hook at DistributionCreator.feeRecipient()

    cfg = {
        "network": NETWORK,
        "deployer": str(deployer),
        "dao": DAO,
        "crvusd": CRVUSD,
        "factory": FACTORY,
        "fee_distributor": fee_distributor,
        "sink_lp": SINK_LP,
        "susds": SUSDS,
        "net_pressure_oracle": oracle.address,
        "market_rate_getter": mrate.address,
        "lt_swap_zap": zap.address,
        "merkl_pid_driver": driver.address,
        "fee_splitter": fs.address,
        "merkl_wrapper": wrapper.address,
        "distribution_creator": DISTRIBUTION_CREATOR,
        "split_fraction": SPLIT_FRACTION,
        "pressure_market_ids": MARKET_IDS,
        "deprecated_market_ids": DEPRECATED_MARKETS,
    }
    with open(json_path, "w") as f:
        json.dump(cfg, f, indent=2)

    print("\n=== part 1: deployed net-pressure Merkl stack ===")
    for k in ("net_pressure_oracle", "market_rate_getter", "lt_swap_zap", "merkl_pid_driver",
              "fee_splitter", "fee_distributor", "merkl_wrapper"):
        print(f"  {k:22s}: {cfg[k]}")
    print(f"  wrapper holder/token   : {driver.address} (driver) / crvUSD")
    print(f"  wrapper distributor    : {DISTRIBUTION_CREATOR} (Merkl DistributionCreator)")
    print(f"  pressure LTs           : {pressure_lts}")
    print(f"  split_fraction         : {SPLIT_FRACTION/1e18:.0%} -> PID, rest -> FeeDistributor")
    print(f"  driver/splitter owner  : {DAO} (DAO)")
    print(f"  addresses written to   : {json_path}")
    print("  next: ask Merkl to whitelist the wrapper, then set_merkl(DistributionCreator, wrapper) on the driver")
    return cfg


# --- part 2: the DAO vote ----------------------------------------------------

def load_contracts(cfg):
    """Load by address every contract the vote touches (same whether just deployed or read from JSON)."""
    factory = boa.load_partial(cpath('contracts/Factory.vy')).at(cfg["factory"])
    fd = boa.load_partial(cpath('contracts/dao/FeeDistributor.vy')).at(cfg["fee_distributor"])
    driver = boa.load_partial(cpath('contracts/net_pressure/MerklPIDDriver.vy')).at(cfg["merkl_pid_driver"])
    fs = boa.load_partial(cpath('contracts/net_pressure/FeeSplitter.vy')).at(cfg["fee_splitter"])
    zap = boa.load_partial(cpath('contracts/utils/LTSwapZap.vy')).at(cfg["lt_swap_zap"])
    crvusd = boa.loads_abi(ERC20_ABI).at(cfg["crvusd"])
    factory_owner = boa.loads_abi(FACTORY_OWNER_ABI).at(factory.admin())   # DAO-controlled proxy
    lt_d = boa.load_partial(cpath('contracts/LT.vy'))
    deprecated_lts = [lt_d.at(factory.markets(i).lt) for i in cfg["deprecated_market_ids"]]
    return SimpleNamespace(factory=factory, factory_owner=factory_owner, fd=fd, driver=driver,
                           fs=fs, zap=zap, crvusd=crvusd, deprecated_lts=deprecated_lts)


def seeding_calls(c):
    """recover -> approve -> convert per deprecated market (crvUSD lands in the DAO). Shared by the
    seed sim and the vote so they stay in sync."""
    calls = []
    for lt in c.deprecated_lts:
        calls.append(Call(c.fd, "recover_token", (lt.address, DAO), f"fd.recover_token({lt.address}, DAO)"))
        calls.append(Call(lt, "approve", (c.zap.address, MAX_UINT), f"{lt.address}.approve(zap, max)"))
        calls.append(Call(c.zap, "convert", (lt.address,), f"zap.convert({lt.address})"))
    return calls


def simulate_seed(c):
    """Size the forward: the crvUSD the DAO nets from recover+zap of markets 0-2, measured in an
    anchor (rolled back). Exact on a same-block fork; a close estimate live - the share amount is
    stable (these markets aren't harvested) but the zap's crvUSD output moves with the pool price."""
    calls = seeding_calls(c)
    with boa.env.anchor():
        before = c.crvusd.balanceOf(DAO)
        with boa.env.prank(DAO):
            for call in calls:
                getattr(call.contract, call.method)(*call.args)
        return c.crvusd.balanceOf(DAO) - before


def build_vote_calls(c, seed):
    """Ordered proposal actions: seed the reserve from the deprecated markets, then install the
    splitter as fee_receiver (connecting the driver)."""
    calls = seeding_calls(c)
    if seed > 0:
        calls.append(Call(c.crvusd, "transfer", (c.driver.address, seed),
                          f"crvusd.transfer(driver, {seed})  # forward the seed to the reserve"))
    # Through the DAO-controlled Factory-owner proxy (msg.sender must be its ADMIN == DAO).
    calls.append(Call(c.factory_owner, "set_fee_receiver", (c.fs.address,),
                      f"factory_owner.set_fee_receiver(fs)  [{c.factory_owner.address}]"))
    return calls


def print_actions(calls):
    print("\n=== part 2: vote actions (executed from the DAO) ===")
    for i, call in enumerate(calls):
        data = getattr(call.contract, call.method).prepare_calldata(*call.args)
        prefix = data.hex()[:10] if isinstance(data, (bytes, bytearray)) else str(data)[:10]
        print(f"  [{i:2d}] to={call.contract.address}  {call.label}  (0x{prefix})")


def simulate_vote(c, calls, seed):
    """Fork-mode: enact the vote as the DAO, then assert it's enabled and the reserve is seeded."""
    with boa.env.prank(DAO):
        for call in calls:
            getattr(call.contract, call.method)(*call.args)

    assert c.factory.fee_receiver() == c.fs.address, "FeeSplitter not installed as fee_receiver"
    assert c.fs.pid() == c.driver.address, "FeeSplitter.pid() != driver"
    assert c.driver.connected() is True, "driver.connected() must be true once the splitter is in"
    assert c.crvusd.balanceOf(c.driver.address) == seed, "driver reserve != seeded amount"
    print(f"\nenabled: fee_receiver -> FeeSplitter, driver connected, "
          f"reserve seeded with {seed/1e18:,.2f} crvUSD from markets 0-2")

    # Smoke: a permissionless trigger() realizes fresh fees and converts the driver's share (market 7 has fees here).
    before = c.crvusd.balanceOf(c.driver.address)
    c.fs.trigger()
    after = c.crvusd.balanceOf(c.driver.address)
    assert after > before, "FeeSplitter.trigger did not grow the reserve from live fees"
    print(f"smoke: FeeSplitter.trigger() added {(after-before)/1e18:,.4f} crvUSD "
          f"(reserve now {after/1e18:,.2f})")


def create_proposal(c, calls):
    """Prod-mode: pin metadata and create the real Aragon proposal from yb-deployer."""
    voting = boa.load_abi(os.path.join(VOTING, "TokenVoting.abi.json"), name="AragonVoting").at(VOTING_PLUGIN)
    actions = [Action(to=call.contract.address, value=0,
                      data=getattr(call.contract, call.method).prepare_calldata(*call.args))
               for call in calls]
    metadata = pin_to_ipfs({
        "title": "Enable net-pressure Merkl incentives",
        "summary": "Install the FeeSplitter as the Factory fee_receiver (connecting the "
                   "MerklPIDDriver) and seed the driver's crvUSD reserve from the deprecated "
                   "markets 0-2 (recover their LT fees and convert via LTSwapZap).",
        "resources": []})
    proposal_id = voting.createProposal(*Proposal(
        metadata=metadata.encode(), actions=actions, allowFailureMap=0,
        startDate=0, endDate=0, voteOption=0, tryEarlyExecution=True))
    print(f"\ncreated proposal: {proposal_id}")
    return proposal_id


# --- entry points ------------------------------------------------------------

def run_fork_e2e():
    """Both parts on one fork: deploy -> read the JSON like prod -> enact the vote -> assert."""
    with boa.fork(NETWORK, block_identifier=FORK_BLOCK):
        boa.env.eoa = DEPLOYER
        deploy_stack(TMP_JSON)

        cfg = json.load(open(TMP_JSON))                # read it back the way part 2 will in prod
        c = load_contracts(cfg)
        assert c.factory_owner.ADMIN() == DAO, "expected the DAO to control the Factory owner proxy"
        seed = simulate_seed(c)
        calls = build_vote_calls(c, seed)
        print_actions(calls)
        simulate_vote(c, calls, seed)


def run_fork_vote():
    """Fork at head and simulate the vote against the ALREADY-DEPLOYED contracts (read from
    DEPLOY_JSON) - no deploy. Enacts the vote as the DAO and asserts, like run_fork_e2e's tail."""
    cfg = json.load(open(DEPLOY_JSON))
    with boa.fork(NETWORK, block_identifier="latest"):
        c = load_contracts(cfg)
        assert c.factory_owner.ADMIN() == DAO, "expected the DAO to control the Factory owner proxy"
        seed = simulate_seed(c)
        calls = build_vote_calls(c, seed)
        print_actions(calls)
        simulate_vote(c, calls, seed)


def run_prod_deploy():
    boa.set_network_env(NETWORK)
    boa.env.add_account(load_deployer())
    deploy_stack(DEPLOY_JSON, verifier=Etherscan(api_key=ETHERSCAN_API_KEY))
    print("\nnext: set STAGE = \"vote\" and re-run to create the DAO proposal.")


def run_prod_vote():
    cfg = json.load(open(DEPLOY_JSON))
    # Size the seed on a throwaway fork. The forward is a fixed amount drawn from the DAO's crvUSD,
    # so a small pool-price move before execution is absorbed by the treasury rather than reverting.
    with boa.fork(NETWORK, block_identifier="latest"):
        seed = simulate_seed(load_contracts(cfg))
    print(f"simulated seed from markets 0-2: {seed/1e18:,.2f} crvUSD")

    boa.set_network_env(NETWORK)
    boa.env.add_account(load_deployer())
    c = load_contracts(cfg)
    calls = build_vote_calls(c, seed)
    print_actions(calls)
    create_proposal(c, calls)


if __name__ == '__main__':
    if FORK:
        run_fork_vote() if STAGE == "vote" else run_fork_e2e()
    elif STAGE == "deploy":
        run_prod_deploy()
    elif STAGE == "vote":
        run_prod_vote()
    else:
        raise SystemExit(f"unknown STAGE {STAGE!r} (use 'deploy' or 'vote')")
