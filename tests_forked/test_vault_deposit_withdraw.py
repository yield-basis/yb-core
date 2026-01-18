import boa
import pytest
from hypothesis import given, settings, assume
from hypothesis import strategies as st
from tests_forked.conftest import WBTC, WETH, CRVUSD


@pytest.fixture(scope="module")
def vault(hybrid_vault_factory, funded_account, factory):
    """Create a HybridVault for the funded_account."""
    with boa.env.prank(funded_account):
        vault_addr = hybrid_vault_factory.create_vault()
    return boa.load_partial("contracts/HybridVault.vy").at(vault_addr)


@pytest.fixture(scope="module")
def erc20(forked_env):
    """ERC20 interface for token interactions."""
    return boa.load_partial("contracts/testing/ERC20Mock.vy")


@pytest.fixture(scope="module")
def wbtc(erc20):
    return erc20.at(WBTC)


@pytest.fixture(scope="module")
def weth(erc20):
    return erc20.at(WETH)


@pytest.fixture(scope="module")
def crvusd(erc20):
    return erc20.at(CRVUSD)


@pytest.fixture(scope="module")
def setup_approvals(vault, funded_account, wbtc, weth, crvusd):
    """Approve vault to spend user's tokens."""
    with boa.env.prank(funded_account):
        wbtc.approve(vault.address, 2**256 - 1)
        weth.approve(vault.address, 2**256 - 1)
        crvusd.approve(vault.address, 2**256 - 1)


@settings(max_examples=20, deadline=None)
@given(
    assets=st.integers(min_value=10**4, max_value=10**8),  # 0.0001 to 1 WBTC
    crvusd_amount=st.integers(min_value=0, max_value=1_000_000 * 10**18)
)
def test_deposit_withdraw_wbtc(
    vault, funded_account, wbtc, crvusd, setup_approvals, factory, assets, crvusd_amount
):
    """Test deposit and withdraw with WBTC (pool 3) using random amounts."""
    pool_id = 3

    # Check this pool uses WBTC
    market = factory.markets(pool_id)
    assume(market.asset_token == WBTC)

    with boa.env.prank(funded_account):
        # Check how much crvUSD is needed for this deposit
        crvusd_needed = vault.crvusd_for_deposit(pool_id, assets, 0)

        # Reset crvUSD balance to the test amount
        current_balance = crvusd.balanceOf(funded_account)
        if current_balance > 0:
            crvusd.transfer(boa.env.generate_address(), current_balance)
        boa.deal(crvusd, funded_account, crvusd_amount)

        if crvusd_amount >= crvusd_needed:
            # Should succeed
            shares = vault.deposit(pool_id, assets, 0, 0, False, True)
            assert shares > 0

            # Withdraw all shares
            vault.withdraw(pool_id, shares, 0, False, funded_account, False)
        else:
            # Should fail due to insufficient crvUSD
            with boa.reverts():
                vault.deposit(pool_id, assets, 0, 0, False, True)


@settings(max_examples=20, deadline=None)
@given(
    assets=st.integers(min_value=10**15, max_value=10**18),  # 0.001 to 1 WETH
    crvusd_amount=st.integers(min_value=0, max_value=1_000_000 * 10**18)
)
def test_deposit_withdraw_weth(
    vault, funded_account, weth, crvusd, setup_approvals, factory, assets, crvusd_amount
):
    """Test deposit and withdraw with WETH (pool 6) using random amounts."""
    pool_id = 6

    # Check this pool uses WETH
    market = factory.markets(pool_id)
    assume(market.asset_token == WETH)

    with boa.env.prank(funded_account):
        # Check how much crvUSD is needed for this deposit
        crvusd_needed = vault.crvusd_for_deposit(pool_id, assets, 0)

        # Reset crvUSD balance to the test amount
        current_balance = crvusd.balanceOf(funded_account)
        if current_balance > 0:
            crvusd.transfer(boa.env.generate_address(), current_balance)
        boa.deal(crvusd, funded_account, crvusd_amount)

        if crvusd_amount >= crvusd_needed:
            # Should succeed
            shares = vault.deposit(pool_id, assets, 0, 0, False, True)
            assert shares > 0

            # Withdraw all shares
            vault.withdraw(pool_id, shares, 0, False, funded_account, False)
        else:
            # Should fail due to insufficient crvUSD
            with boa.reverts():
                vault.deposit(pool_id, assets, 0, 0, False, True)
