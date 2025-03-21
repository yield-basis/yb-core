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
    factory = boa.load(
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
    with boa.env.prank(admin):
        factory.set_mint_factory(admin)
        stablecoin._mint_for_testing(factory.address, 1000 * 10**6 * 10**18)
    return factory


def test_factory(factory, admin, accounts, stablecoin):
    with boa.env.prank(admin):
        with boa.reverts('Only set once'):
            factory.set_mint_factory(accounts[0])
    with boa.env.prank(accounts[0]):
        with boa.reverts('Access'):
            factory.set_mint_factory(accounts[0])
    with boa.env.prank(admin):
        # Take coins back to the "minter"
        stablecoin.transferFrom(factory.address, admin, stablecoin.balanceOf(factory.address))
    with boa.env.prank(accounts[0]):
        with boa.reverts('Access'):
           factory.set_allocator(accounts[0], 10**18)
    with boa.env.prank(admin):
        with boa.reverts('Minter'):
           factory.set_allocator(admin, 10**18)
    with boa.env.prank(accounts[0]):
        stablecoin.approve(factory.address, 2**256-1)
    with boa.env.prank(admin):
        stablecoin._mint_for_testing(accounts[0], 10**18)
        factory.set_allocator(accounts[0], 10**18)


def test_create_market(factory, cryptopool, seed_cryptopool, accounts, admin):
    fee = int(0.007e18)
    rate = int(0.1e18 / (365 * 86400))
    ceiling = 100 * 10**6 * 10**18

    with boa.reverts('Access'):
        with boa.env.prank(accounts[0]):
            factory.add_market(cryptopool.address, fee, rate, ceiling)

    with boa.env.prank(admin):
        factory.add_market(cryptopool.address, fee, rate, ceiling)
