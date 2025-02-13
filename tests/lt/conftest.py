import boa
import pytest


@pytest.fixture(scope="session")
def cryptopool(stablecoin, collateral_token, admin, accounts):
    with boa.env.prank(admin):
        amm_interface = boa.load_partial('contracts/twocrypto/CurveTwocryptoOptimized.vy')
        amm_impl = amm_interface.deploy_as_blueprint()
        math_impl = boa.load_partial('contracts/twocrypto/CurveCryptoMathOptimized2.vy').deploy_as_blueprint()
        views_impl = boa.load_partial('contracts/twocrypto/CurveCryptoViews2Optimized.vy').deploy_as_blueprint()
        gauge_impl = "0x0000000000000000000000000000000000000000"

        factory = boa.load('contracts/twocrypto/CurveTwocryptoFactory.vy')
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


@pytest.fixture(scope="session")
def cryptopool_oracle(cryptopool):
    return boa.load('contracts/CryptopoolLPOracle.vy', cryptopool.address)


@pytest.fixture(scope="session")
def yb_lt(amm_deployer, cryptopool, cryptopool_oracle, collateral_token, stablecoin, accounts, admin):
    with boa.env.prank(admin):
        lt = boa.load(
            'contracts/LT.vy',
            collateral_token.address,
            stablecoin.address,
            cryptopool.address,
            admin)

        amm = amm_deployer.deploy(
            lt.address,
            stablecoin.address,
            collateral_token.address,
            2 * 10**18,  # leverage = 2.0
            int(0.007e18),  # fee
            cryptopool_oracle.address
        )
        lt.set_amm(amm.address)

        for addr in accounts + [admin]:
            with boa.env.prank(addr):
                stablecoin.approve(lt.address, 2**256-1)
                collateral_token.approve(lt.address, 2**256-1)
                cryptopool.approve(amm.address, 2**256-1)
                stablecoin.approve(amm.address, 2**256-1)

        return lt
