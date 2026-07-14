"""
Fork test: MerklPIDDriver against the LIVE Merkl DistributionCreator + Distributor, using the
wrapper exactly as Merkl deploys it: an ERC1967Proxy over Merkl's on-chain, audited, verified
PullTokenWrapper implementation, initialized for crvUSD with the driver as holder.

The proxy is a standard OpenZeppelin ERC1967Proxy (vendored bytecode, scripts/erc1967_proxy.json -
matches Merkl's canonical build, so a real deploy is an Etherscan exact-match); the logic is
Merkl's already-deployed impl, so nothing wrapper-specific is compiled or vendored here. We then
drive set_merkl / accept_conditions / create_campaign / override_campaign for real - including
the wrapper's real claim hook pulling the crvUSD fee out of the driver at creation. What this
can NOT cover is Merkl's off-chain engine reading preview_target_apr; that's their side.

We override the repo's Jan-2026 FORK_BLOCK: Merkl upgraded the DistributionCreator impl since
then (0x9b2f11ea -> 0xe9dac26), so we fork at head to match the ABI this contract targets.
"""
import json
import boa
import pytest
from eth_abi import encode
from eth_utils import keccak
from tests_forked.networks import NETWORK

CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
SCRVUSD = "0x0655977FEb2f289A4aB78af67BAB0d17aAb84367"      # crvUSD whale we deal the fee from
DC = "0x8BB4C975Ff3c250e0ceEA271728547f3802B36Fd"           # Merkl DistributionCreator (proxy)
DISTRIBUTOR = "0x3Ef3D8bA38EBe18DB133cEc108f4D14CE00Dd9Ae"  # Merkl Distributor
PULL_IMPL = "0x979a04fd2f3a6a2b3945a715e24b974323e93567"    # Merkl's verified PullTokenWrapper impl

DC_ABI = json.dumps([
 {"name": "accessControlManager", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "feeRecipient", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "messageHash", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "bytes32"}]},
 {"name": "defaultFees", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
 {"name": "userSignatures", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "bytes32"}]},
 {"name": "setRewardTokenMinAmounts", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "address[]"}, {"type": "uint256[]"}], "outputs": []},
])
ACM_ABI = json.dumps([
 {"name": "getRoleMember", "stateMutability": "view", "type": "function", "inputs": [{"type": "bytes32"}, {"type": "uint256"}], "outputs": [{"type": "address"}]},
])
ERC20_ABI = json.dumps([
 {"name": "transfer", "stateMutability": "nonpayable", "type": "function", "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
 {"name": "balanceOf", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
])
WRAPPER_ABI = json.dumps([
 {"name": "token", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "holder", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "distributor", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
 {"name": "balanceOf", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
 {"name": "setFeeRecipient", "stateMutability": "nonpayable", "type": "function", "inputs": [], "outputs": []},
])


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    """Override the repo's pinned FORK_BLOCK: fork at head so the live Merkl impls match the
    ABI this contract targets."""
    with boa.fork(NETWORK, block_identifier="latest"):
        yield


def _deploy_real_wrapper(token, distribution_creator, holder):
    """Deploy a Merkl crvUSD wrapper exactly as Merkl does: ERC1967Proxy over their on-chain
    PullTokenWrapper impl, initialize(token, distributionCreator, holder, name, symbol)."""
    proxy_bytecode = bytes.fromhex(json.load(open("scripts/erc1967_proxy.json"))["bytecode"][2:])
    init = keccak(text="initialize(address,address,address,string,string)")[:4] + encode(
        ["address", "address", "address", "string", "string"],
        [token, distribution_creator, holder, "Yield Basis crvUSD (wrapped)", "ybwcrvUSD"])
    ctor = encode(["address", "bytes"], [PULL_IMPL, init])
    addr = boa.env.deploy_code(bytecode=proxy_bytecode + ctor)
    if isinstance(addr, tuple):
        addr = addr[0]
    wrapper = boa.loads_abi(WRAPPER_ABI).at(addr)
    wrapper.setFeeRecipient()   # point the wrapper's fee hook at DC.feeRecipient()
    return wrapper


def test_merkl_fork():
    admin = boa.env.generate_address("admin")
    ph = boa.env.generate_address("ph")     # placeholders for the PID wiring (unused by Merkl fns)
    driver = boa.load("contracts/net_pressure/MerklPIDDriver.vy", CRVUSD, ph, ph, ph, ph, admin)
    crvusd = boa.loads_abi(ERC20_ABI).at(CRVUSD)
    dc = boa.loads_abi(DC_ABI).at(DC)
    acm = boa.loads_abi(ACM_ABI).at(dc.accessControlManager())

    # Deploy the real crvUSD wrapper (proxy -> live impl, holder = driver) and sanity-check it.
    wrapper = _deploy_real_wrapper(CRVUSD, DC, driver.address)
    assert wrapper.token() == CRVUSD
    assert wrapper.holder() == driver.address
    assert wrapper.distributor() == DISTRIBUTOR

    # 1) a Merkl guardian whitelists the wrapper; 2) deal crvUSD to the driver for the fee
    guardian = acm.getRoleMember(keccak(text="GUARDIAN_ROLE"), 0)
    with boa.env.prank(guardian):
        dc.setRewardTokenMinAmounts([wrapper.address], [10**15])
    with boa.env.prank(SCRVUSD):
        crvusd.transfer(driver.address, 100 * 10**18)

    # 3) DAO installs Merkl + the wrapper and accepts Merkl's terms (real calls)
    with boa.env.prank(admin):
        driver.set_merkl(DC, wrapper.address)
        driver.accept_conditions()
    assert dc.userSignatures(driver.address) == dc.messageHash()   # so hasSigned passes

    # 4) create a campaign for real: mint(amount) -> wrapper mints to the holder, the
    #    DistributionCreator pulls it, and the real claim hook pulls the crvUSD fee out of us.
    fee_recipient = dc.feeRecipient()
    amount, dur = 1000 * 10**18, 7 * 86400
    fee = amount * dc.defaultFees() // 10**9
    d_crv0, f_crv0 = crvusd.balanceOf(driver.address), crvusd.balanceOf(fee_recipient)
    dist0 = wrapper.balanceOf(DISTRIBUTOR)
    now = boa.env.evm.patch.timestamp
    with boa.env.prank(admin):
        cid = driver.create_campaign(amount, 2, now, dur, bytes(range(48)))
    assert cid != b"\x00" * 32
    assert wrapper.balanceOf(driver.address) == 0                   # minted then fully pulled
    assert wrapper.balanceOf(DISTRIBUTOR) - dist0 == amount - fee   # net wrapper escrowed for claims
    # crvUSD stayed in the driver except the fee, which the wrapper pulled to the fee recipient:
    assert d_crv0 - crvusd.balanceOf(driver.address) == fee
    assert crvusd.balanceOf(fee_recipient) - f_crv0 == fee

    # 5) override the same campaign for real (reverts if the campaign/creator were wrong)
    with boa.env.prank(admin):
        driver.override_campaign(cid, 2, now, dur, bytes(range(20)))
