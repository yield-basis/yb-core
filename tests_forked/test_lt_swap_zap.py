"""
Fork test of LTSwapZap: recover the deprecated markets 0-2 fee shares from the live
FeeDistributor to the DAO, which approves the zap and calls convert(lt) once per token. Each
call pulls the caller's shares (transferFrom), withdraws them to the pool asset and swaps that
to crvUSD (oracle-bounded), sending the crvUSD to the caller. The swap is best-effort: if it
can't meet its on-chain min_dy, the error is swallowed and the withdrawn asset is handed back
to the caller instead (convert returns 0). A too-tight withdraw floor reverts the whole call,
so the caller never loses its shares.
"""
import boa
import pytest
from tests_forked.networks import NETWORK

ZAP_BLOCK = 25496670
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
CRVUSD = "0xf939E0A03FB07F59A73314E73794Be0E57ac1b4E"
FEE_DISTRIBUTOR = "0xD11b416573EbC59b6B2387DA0D2c0D1b3b1F7A90"
FD_OWNER = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"
DEPRECATED = [0, 1, 2]

ERC20_ABI = """[
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"coins","outputs":[{"type":"address"}],"inputs":[{"type":"uint256","name":"i"}],"stateMutability":"view","type":"function"},
 {"name":"CRYPTOPOOL","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


@pytest.fixture(autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier=ZAP_BLOCK):
        yield


def _at(addr):
    return boa.loads_abi(ERC20_ABI).at(addr)


def _setup(swap_fee_multiplier, dao):
    """Deploy the zap, recover the deprecated markets' fee shares from the FeeDistributor to the
    DAO, and have the DAO approve the zap to pull them. Returns (zap, [lt addrs], [dao shares])."""
    factory = boa.load_partial("contracts/Factory.vy").at(FACTORY)
    oracle = boa.load("contracts/net_pressure/YBNetPressure.vy")
    zap = boa.load("contracts/utils/LTSwapZap.vy", CRVUSD, oracle.address,
                   swap_fee_multiplier, dao)
    fd = boa.load_partial("contracts/dao/FeeDistributor.vy").at(FEE_DISTRIBUTOR)
    lts = [factory.markets(i).lt for i in DEPRECATED]
    with boa.env.prank(FD_OWNER):
        for lt in lts:
            fd.recover_token(lt, dao)                  # owner-only; token_balances==0 -> full balance
    shares = [_at(lt).balanceOf(dao) for lt in lts]
    with boa.env.prank(dao):                           # caller approves the zap to pull its shares
        for lt in lts:
            _at(lt).approve(zap.address, 2**256 - 1)
    return zap, lts, shares


def test_zap_converts_to_caller():
    dao = boa.env.generate_address()
    crvusd = _at(CRVUSD)

    zap, lts, shares = _setup(3 * 10**18 // 2, dao)    # 1.5x: enough slippage room to convert all
    assert all(s > 0 for s in shares), "expected fee shares to recover"

    before = crvusd.balanceOf(dao)
    total = 0
    with boa.env.prank(dao):
        for lt in lts:
            total += zap.convert(lt)                    # one LT per call; crvUSD -> caller
    got = crvusd.balanceOf(dao) - before

    print(f"\nrecovered shares: {[s/1e18 for s in shares]}")
    print(f"realized to caller: {got/1e18:,.2f} crvUSD (returned {total/1e18:,.2f})")

    assert total == got and got > 0, "convert must send its returned crvUSD to the caller"
    # The zap is transient - it keeps neither the LT shares nor the crvUSD.
    for lt in lts:
        assert _at(lt).balanceOf(zap.address) == 0, "zap kept LT shares"
    assert crvusd.balanceOf(zap.address) == 0, "zap kept crvUSD"


def test_zap_returns_asset_when_swap_cannot_meet_min():
    """At 0.5x the thin yb-tBTC pool can't meet its swap min_dy: the exchange error is swallowed
    and the withdrawn pool asset is handed to the caller (convert returns 0), while the deeper
    pools still convert to crvUSD. The zap keeps neither shares, asset, nor crvUSD."""
    dao = boa.env.generate_address()
    crvusd = _at(CRVUSD)

    zap, lts, _ = _setup(5 * 10**17, dao)              # 0.5x
    swallowed = converted = 0
    for lt in lts:
        asset = _at(_at(lt).CRYPTOPOOL()).coins(1)
        dao_asset_before = _at(asset).balanceOf(dao)
        crv_before = crvusd.balanceOf(dao)
        with boa.env.prank(dao):
            out = zap.convert(lt)
        if out == 0:
            swallowed += 1
            # swap swallowed: the withdrawn asset came back to the caller, and no crvUSD.
            assert _at(asset).balanceOf(dao) - dao_asset_before > 0, "asset not returned to caller"
            assert crvusd.balanceOf(dao) == crv_before, "caller got crvUSD despite a swallowed swap"
        else:
            converted += 1
            assert crvusd.balanceOf(dao) - crv_before == out, "crvUSD not delivered to caller"
        # The zap holds nothing after each call.
        assert _at(lt).balanceOf(zap.address) == 0
        assert _at(asset).balanceOf(zap.address) == 0
        assert crvusd.balanceOf(zap.address) == 0

    assert swallowed >= 1 and converted >= 1, f"expected a mix; {converted} converted, {swallowed} swallowed"


def test_zap_withdraw_floor_reverts_keeping_caller_shares():
    """At 0x the withdraw floor (min_assets = full oracle-fair value) can't be met, so the whole
    convert reverts - the caller keeps its shares and nothing is lost to the zap."""
    dao = boa.env.generate_address()
    zap, lts, shares = _setup(0, dao)
    lt, s = lts[0], shares[0]
    with boa.env.prank(dao):
        with boa.reverts():
            zap.convert(lt)
    assert _at(lt).balanceOf(dao) == s, "caller must keep its shares when convert reverts"
    assert _at(lt).balanceOf(zap.address) == 0
