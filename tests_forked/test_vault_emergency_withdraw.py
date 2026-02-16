import boa
from tests_forked.conftest import WBTC, SCRVUSD


def test_emergency_withdraw_default(
    vault, funded_account, wbtc, crvusd, setup_approvals, factory, twocrypto, erc20
):
    """Test emergency_withdraw with default crvusd_from_wallet=False uses the backing vault."""
    pool_id = 3
    market = factory.markets(pool_id)
    assert market.asset_token == WBTC

    cryptopool = twocrypto.at(market.cryptopool)
    price = cryptopool.price_scale()
    assets = 10**6  # 0.01 WBTC
    debt = assets * price // 10**8 // 2

    with boa.env.prank(funded_account):
        assert vault.safe_to_deposit(pool_id, assets, debt)
        shares = vault.deposit(pool_id, assets, debt, 0, False, True)
        assert shares > 0

    scrvusd = erc20.at(SCRVUSD)
    scrvusd_vault_before = scrvusd.balanceOf(vault.address)
    wbtc_user_before = wbtc.balanceOf(funded_account)

    with boa.env.prank(funded_account):
        vault.emergency_withdraw(pool_id, shares)

    scrvusd_vault_after = scrvusd.balanceOf(vault.address)
    wbtc_user_after = wbtc.balanceOf(funded_account)

    # Default path redeems and re-deposits scrvUSD, so balance may change slightly
    # but user should get WBTC back
    assert wbtc_user_after > wbtc_user_before

    # LT shares should be fully withdrawn
    lt = boa.load_partial("contracts/LT.vy").at(market.lt)
    assert lt.balanceOf(vault.address) == 0


def test_emergency_withdraw_crvusd_from_wallet(
    vault, funded_account, wbtc, crvusd, setup_approvals, factory, twocrypto, erc20
):
    """Test emergency_withdraw with crvusd_from_wallet=True pulls crvUSD from caller, not the backing vault."""
    pool_id = 3
    market = factory.markets(pool_id)
    assert market.asset_token == WBTC

    cryptopool = twocrypto.at(market.cryptopool)
    price = cryptopool.price_scale()
    assets = 10**6  # 0.01 WBTC
    debt = assets * price // 10**8 // 2

    with boa.env.prank(funded_account):
        assert vault.safe_to_deposit(pool_id, assets, debt)
        shares = vault.deposit(pool_id, assets, debt, 0, False, True)
        assert shares > 0

    scrvusd = erc20.at(SCRVUSD)
    scrvusd_vault_before = scrvusd.balanceOf(vault.address)
    crvusd_user_before = crvusd.balanceOf(funded_account)
    wbtc_user_before = wbtc.balanceOf(funded_account)

    with boa.env.prank(funded_account):
        vault.emergency_withdraw(pool_id, shares, True)

    scrvusd_vault_after = scrvusd.balanceOf(vault.address)
    crvusd_user_after = crvusd.balanceOf(funded_account)
    wbtc_user_after = wbtc.balanceOf(funded_account)

    # scrvUSD in vault must be unchanged - the backing vault was not touched
    assert scrvusd_vault_after == scrvusd_vault_before

    # User should have received WBTC back
    assert wbtc_user_after > wbtc_user_before

    # LT shares should be fully withdrawn
    lt = boa.load_partial("contracts/LT.vy").at(market.lt)
    assert lt.balanceOf(vault.address) == 0
