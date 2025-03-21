import pytest
import boa

from eth.constants import ZERO_ADDRESS


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
def flash(stablecoin):
    return boa.load('contracts/testing/FlashLender.vy', stablecoin.address, 10**12 * 10**18)


@pytest.fixture(scope="session")
def factory(stablecoin, amm_impl, lt_impl, vpool_impl, oracle_impl, mock_agg, flash, admin):
    return boa.load(
        'contracts/Factory.vy',
        stablecoin.address,
        amm_impl.address,
        lt_impl.address,
        vpool_impl.address,
        oracle_impl.address,
        ZERO_ADDRESS.hex(),  # Staker
        mock_agg.address,
        flash.address,
        admin,  # Fee receiver
        admin)  # Admin


def test_factory(factory):
    pass
