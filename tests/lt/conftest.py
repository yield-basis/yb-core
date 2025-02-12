import boa
import pytest


@pytest.fixture(scope="session")
def cryptopool(stablecoin, collateral_token, admin):
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
        return factory.deploy_pool(
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
        )


@pytest.fixutre(scope="session")
def cryptopool_oracle():
    pass


@pytest.fixture(scope="session")
def yb_liquidity():
    pass
