import boa
import pytest
from tests_forked.networks import NETWORK

# LTDepositZap into WETH m10 once Curve vote 1508 attaches YBOraclizedPolicy to
# its cryptopool. The policy's LT gate admits a deposit only at the LevAMM
# book's debt / collateral ratio (to 0.01%), which an off-chain quote misses
# once the pool trades before inclusion.

FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"
WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
NEW_POLICY = "0xAEa95d551154443E73565bE724d59D63d72f8A7d"  # set by Curve vote 1508
NEW_RPF = 43 * 10**10 // 100
MARKET = 10
CRVUSD_SOURCE_MARKET = 7  # WBTC AMM: idle crvUSD for the trader, unrelated to m10
PREVIEW_PRECISION = 10**9  # preview vs result: rounding only

ERC20_ABI = """[
 {"name":"transfer","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"to"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"approve","outputs":[{"type":"bool"}],"inputs":[{"type":"address","name":"s"},{"type":"uint256","name":"v"}],"stateMutability":"nonpayable","type":"function"},
 {"name":"balanceOf","outputs":[{"type":"uint256"}],"inputs":[{"type":"address","name":"a"}],"stateMutability":"view","type":"function"},
 {"name":"deposit","outputs":[],"inputs":[],"stateMutability":"payable","type":"function"}
]"""
ADMIN_ABI = '[{"name":"admin","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"}]'


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier="latest"):
        yield


@pytest.fixture(scope="module")
def factory():
    return boa.load_partial("contracts/Factory.vy").at(FACTORY)


@pytest.fixture(scope="module")
def market(factory):
    m = factory.markets(MARKET)
    return dict(
        lt=boa.load_partial("contracts/LT.vy").at(m.lt),
        gauge=boa.loads_abi(ERC20_ABI).at(m.staker),
        amm=boa.load_partial("contracts/AMM.vy").at(m.amm),
        pool=boa.load_partial("contracts/twocrypto_pool/contracts/main/Twocrypto.vy").at(m.cryptopool),
    )


@pytest.fixture(scope="module")
def curve_admin(market):
    return boa.loads_abi(ADMIN_ABI).at(market["pool"].factory()).admin()


@pytest.fixture(scope="module", autouse=True)
def policy(market, curve_admin):
    """Curve vote 1508, unless it has executed already"""
    pool = market["pool"]
    if pool.POLICY() != NEW_POLICY:
        with boa.env.prank(curve_admin):
            pool.set_policy_contract(NEW_POLICY)
            pool.set_fee_parameters(NEW_RPF, 0)
    assert pool.POLICY() == NEW_POLICY


@pytest.fixture(scope="module")
def zap():
    return boa.load("contracts/LTDepositZap.vy")


@pytest.fixture(scope="module")
def weth():
    return boa.loads_abi(ERC20_ABI).at(WETH)


@pytest.fixture(scope="module")
def crvusd(market):
    return boa.loads_abi(ERC20_ABI).at(market["pool"].coins(0))


def _fund(weth, spender, eth):
    user = boa.env.generate_address()
    assets = int(eth * 10**18)
    boa.env.set_balance(user, assets)
    with boa.env.prank(user):
        weth.deposit(value=assets)
        weth.approve(spender, 2**256 - 1)
    return user, assets


def _book(market):
    return market["amm"].get_debt(), market["amm"].collateral_amount()


def _assert_book_ratio(market, book_before):
    """The deposit kept the book's ratio, far inside the gate's 0.01%"""
    debt0, collateral0 = book_before
    debt1, collateral1 = _book(market)
    proportional = (collateral1 - collateral0) * debt0 // collateral0
    assert abs((debt1 - debt0) - proportional) <= proportional // 10**6


def _trade(market, weth, crvusd, factory, sell_weth, usd):
    pool = market["pool"]
    trader = boa.env.generate_address()
    if sell_weth:
        amount = usd * 10**36 // pool.price_oracle()
        boa.env.set_balance(trader, amount)
        with boa.env.prank(trader):
            weth.deposit(value=amount)
    else:
        amount = usd * 10**18
        with boa.env.prank(factory.markets(CRVUSD_SOURCE_MARKET).amm):
            crvusd.transfer(trader, amount)
    with boa.env.prank(trader):
        (weth if sell_weth else crvusd).approve(pool.address, 2**256 - 1)
        pool.exchange(1 if sell_weth else 0, 0 if sell_weth else 1, amount, 0)


def _deposit_and_check(zap, market, user, assets):
    lt = market["lt"]
    book = _book(market)
    preview = zap.preview_deposit(lt.address, assets)
    with boa.env.prank(user):
        shares = zap.deposit(lt.address, assets, preview - preview // PREVIEW_PRECISION)
    assert abs(shares - preview) <= preview // PREVIEW_PRECISION
    _assert_book_ratio(market, book)


def test_price_oracle_debt_fails_gate(market, weth):
    lt, pool = market["lt"], market["pool"]
    user, assets = _fund(weth, lt.address, 10)
    with boa.env.prank(user):
        with boa.reverts("LT deviation"):
            lt.deposit(assets, assets * pool.price_oracle() // 10**18, 0)


@pytest.mark.parametrize("stake", [False, True])
@pytest.mark.parametrize("eth", [0.01, 10, 1000])
def test_deposit(zap, market, weth, eth, stake):
    lt = market["lt"]
    user, assets = _fund(weth, zap.address, eth)
    preview = zap.preview_deposit(lt.address, assets, stake)
    book = _book(market)

    with boa.env.prank(user):
        shares = zap.deposit(lt.address, assets, preview - preview // PREVIEW_PRECISION, user, stake)

    assert abs(shares - preview) <= preview // PREVIEW_PRECISION
    assert (market["gauge"] if stake else lt).balanceOf(user) == shares
    assert (lt if stake else market["gauge"]).balanceOf(user) == 0
    assert weth.balanceOf(user) == 0
    assert weth.balanceOf(zap.address) == 0
    assert lt.balanceOf(zap.address) == 0
    _assert_book_ratio(market, book)


@pytest.mark.parametrize("stake", [False, True])
def test_deposit_for_receiver(zap, market, weth, stake):
    lt = market["lt"]
    user, assets = _fund(weth, zap.address, 1)
    receiver = boa.env.generate_address()
    with boa.env.prank(user):
        shares = zap.deposit(lt.address, assets, 0, receiver, stake)
    token = market["gauge"] if stake else lt
    assert token.balanceOf(receiver) == shares > 0
    assert token.balanceOf(user) == 0


@pytest.mark.parametrize("stake", [False, True])
def test_min_shares(zap, market, weth, stake):
    lt = market["lt"]
    user, assets = _fund(weth, zap.address, 1)
    preview = zap.preview_deposit(lt.address, assets, stake)
    with boa.env.prank(user):
        with boa.reverts("Slippage"):
            zap.deposit(lt.address, assets, preview + preview // 10**6, user, stake)


@pytest.mark.parametrize("sell_weth", [True, False])
def test_trade_after_quote(zap, factory, market, weth, crvusd, sell_weth):
    """A quote goes stale once the pool trades in the next block; the zap solves at inclusion"""
    lt = market["lt"]
    user, assets = _fund(weth, zap.address, 10)
    with boa.env.prank(user):
        weth.approve(lt.address, 2**256 - 1)
    quote = zap.calc_debt(lt.address, assets)

    _trade(market, weth, crvusd, factory, sell_weth, 300_000)
    boa.env.time_travel(seconds=12)

    with boa.env.prank(user):
        with boa.reverts("LT deviation"):
            lt.deposit(assets, quote, 0)
    _deposit_and_check(zap, market, user, assets)


@pytest.mark.parametrize("sell_weth", [True, False])
def test_skewed_pool(zap, factory, market, weth, crvusd, sell_weth):
    """The two Newton steps hold with the pool far off balance and price_scale lagging"""
    user, assets = _fund(weth, zap.address, 10)
    _trade(market, weth, crvusd, factory, sell_weth, 3_000_000)
    boa.env.time_travel(seconds=3600)
    _deposit_and_check(zap, market, user, assets)


def test_deposit_without_gate(zap, market, weth, curve_admin):
    """Without a policy any debt passes, and the zap still deposits at the book's ratio"""
    lt, pool = market["lt"], market["pool"]
    with boa.env.prank(curve_admin):
        pool.set_policy_contract("0x0000000000000000000000000000000000000000")
    user, assets = _fund(weth, zap.address, 10)
    book = _book(market)
    with boa.env.prank(user):
        assert zap.deposit(lt.address, assets, 0) > 0
    _assert_book_ratio(market, book)
