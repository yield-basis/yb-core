import os
from datetime import timedelta

import boa
import pytest
from hypothesis import settings


boa.env.enable_fast_mode()


PRICE = 100_000
RESERVE = 10**9
RATE = 10**9 // (365 * 86400)


settings.register_profile("default", deadline=timedelta(seconds=1000))
settings.load_profile(os.getenv(u"HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(scope="session")
def accounts():
    return [boa.env.generate_address() for _ in range(10)]


@pytest.fixture(scope="session")
def admin():
    return boa.env.generate_address()


@pytest.fixture(scope="session")
def token_mock():
    return boa.load_partial('contracts/testing/ERC20Mock.vy')


@pytest.fixture(scope="session")
def collateral_token(token_mock):
    return token_mock.deploy('Collateral', 'xxxBTC', 18)


@pytest.fixture(scope="session")
def stablecoin(token_mock):
    return token_mock.deploy('Stablecoin', 'xxxUSD', 18)


@pytest.fixture(scope="session")
def price_oracle(admin):
    with boa.env.prank(admin):
        oracle = boa.load('contracts/testing/DummyPriceOracle.vy', admin, PRICE * 10**18)
        return oracle


@pytest.fixture(scope="session")
def amm_deployer():
    return boa.load_partial('contracts/AMM.vy')


@pytest.fixture(scope="session")
def amm(amm_deployer, admin, stablecoin, collateral_token, price_oracle, accounts):
    with boa.env.prank(admin):
        amm = amm_deployer.deploy(
                admin,
                stablecoin.address,
                collateral_token.address,
                2 * 10**18,
                int(0.007e18),
                price_oracle.address
        )
        for a in accounts + [admin]:
            with boa.env.prank(a):
                stablecoin.approve(amm.address, 2**256-1)
                collateral_token.approve(amm.address, 2**256-1)
        return amm


@pytest.fixture(scope="session")
def yb(admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/YB.vy', RESERVE, RATE)
