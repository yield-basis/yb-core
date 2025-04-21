import boa
import pytest

from eth.constants import ZERO_ADDRESS


@pytest.fixture(scope="session")
def cryptopool(stablecoin, collateral_token, admin, accounts):
    with boa.env.prank(admin):
        amm_interface = boa.load_partial('contracts/testing/twocrypto/Twocrypto.vy')
        amm_impl = amm_interface.deploy_as_blueprint()
        math_impl = boa.load('contracts/testing/twocrypto/StableswapMath.vy')
        views_impl = boa.load('contracts/testing/twocrypto/TwocryptoView.vy')
        gauge_impl = ZERO_ADDRESS.hex()

        factory = boa.load('contracts/testing/twocrypto/TwocryptoFactory.vy')
        factory.initialise_ownership(admin, admin)
        factory.set_pool_implementation(amm_impl, 0)
        factory.set_gauge_implementation(gauge_impl)
        factory.set_views_implementation(views_impl)
        factory.set_math_implementation(math_impl)

        # Params have nothing to do with reality!
        pool = amm_interface.at(
            factory.deploy_pool(
                "Test pool",  # _name: String[64]
                "TST",  # _symbol: String[32]
                [stablecoin.address, collateral_token.address],
                0,  # implementation_id: uint256
                5 * 10000 * 2**2,  # A: uint256
                int(1e-5 * 1e18),  # gamma: uint256
                int(0.0025 * 1e10),  # mid_fee: uint256
                int(0.0045 * 1e10),  # out_fee: uint256
                int(0.01 * 1e18),  # fee_gamma: uint256
                int(1e-10 * 1e18),  # allowed_extra_profit: uint256
                int(1e-6 * 1e18),  # adjustment_step: uint256
                600,  # ma_exp_time: uint256
                100_000 * 10**18  # initial_price: uint256
            ))

        for addr in accounts + [admin]:
            with boa.env.prank(addr):
                stablecoin.approve(pool.address, 2**256-1)
                collateral_token.approve(pool.address, 2**256-1)

        return pool


@pytest.fixture(scope="function")
def seed_cryptopool(stablecoin, collateral_token, cryptopool, admin):
    stablecoin._mint_for_testing(admin, 100_000 * 10**18)
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        cryptopool.add_liquidity([100_000 * 10**18, 10**18], 0)


@pytest.fixture(scope="session")
def mock_agg(admin):
    return boa.load('contracts/testing/DummyPriceOracle.vy', admin, 10**18)


@pytest.fixture(scope="session")
def amm_interface():
    return boa.load_partial('contracts/AMM.vy')


@pytest.fixture(scope="session")
def amm_impl(amm_interface):
    return amm_interface.deploy_as_blueprint()


@pytest.fixture(scope="session")
def lt_interface():
    return boa.load_partial('contracts/LT.vy')


@pytest.fixture(scope="session")
def lt_impl(lt_interface):
    return lt_interface.deploy_as_blueprint()


@pytest.fixture(scope="session")
def vpool_interface():
    return boa.load_partial('contracts/VirtualPool.vy')


@pytest.fixture(scope="session")
def vpool_impl(vpool_interface):
    return vpool_interface.deploy_as_blueprint()


@pytest.fixture(scope="session")
def oracle_interface():
    return boa.load_partial('contracts/CryptopoolLPOracle.vy')


@pytest.fixture(scope="session")
def oracle_impl(oracle_interface):
    return oracle_interface.deploy_as_blueprint()


@pytest.fixture(scope="session")
def gauge_interface():
    return boa.load_partial('contracts/LiquidityGauge.vy')


@pytest.fixture(scope="session")
def gauge_impl(gauge_interface):
    return gauge_interface.deploy_as_blueprint()


@pytest.fixture(scope="session")
def flash(stablecoin):
    return boa.load('contracts/testing/FlashLender.vy', stablecoin.address, 10**12 * 10**18)


@pytest.fixture(scope="session")
def factory(stablecoin, amm_impl, lt_impl, vpool_impl, oracle_impl, gauge_impl, mock_agg, flash, admin):
    factory = boa.load(
        'contracts/Factory.vy',
        stablecoin.address,
        amm_impl.address,
        lt_impl.address,
        vpool_impl.address,
        oracle_impl.address,
        gauge_impl,
        mock_agg.address,
        flash.address,
        admin,  # Fee receiver
        admin,  # Admin
        admin)  # Emergency admin
    with boa.env.prank(admin):
        factory.set_mint_factory(admin)
        stablecoin._mint_for_testing(factory.address, 10**30)
    return factory


@pytest.fixture(scope="session")
def yb_market(factory, cryptopool, admin):
    fee = int(0.007e18)
    rate = int(0.1e18 / (365 * 86400))
    ceiling = 0

    with boa.env.prank(admin):
        return factory.add_market(cryptopool.address, fee, rate, ceiling)


@pytest.fixture(scope="session")
def yb_amm(amm_interface, yb_market):
    return amm_interface.at(yb_market[2])


@pytest.fixture(scope="session")
def cryptopool_oracle(oracle_interface, yb_market):
    return oracle_interface.at(yb_market[4])


@pytest.fixture(scope="session")
def yb_lt(lt_interface, yb_market, cryptopool, stablecoin, collateral_token, accounts, admin):
    with boa.env.prank(admin):
        lt = yb_market[3]
        amm = yb_market[2]
        for addr in accounts + [admin]:
            with boa.env.prank(addr):
                stablecoin.approve(lt, 2**256-1)
                collateral_token.approve(lt, 2**256-1)
                cryptopool.approve(amm, 2**256-1)
                stablecoin.approve(amm, 2**256-1)

        return lt_interface.at(lt)


@pytest.fixture(scope="function")
def yb_allocated(yb_lt, admin):
    with boa.env.prank(admin):
        yb_lt.allocate_stablecoins(10**30)


@pytest.fixture(scope="session")
def yb_staker(gauge_interface, yb_market, yb_lt, accounts, admin):
    staker = gauge_interface.at(yb_market[6])
    for addr in accounts + [admin]:
        with boa.env.prank(addr):
            yb_lt.approve(staker.address, 2**256-1)
    return staker
