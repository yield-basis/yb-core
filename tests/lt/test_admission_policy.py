import boa
import pytest


RATIO_TOLERANCE = 10**14  # 1 bps


@pytest.fixture
def virtual_pool(factory, flash, vpool_interface, stablecoin, collateral_token, admin, accounts):
    stablecoin._mint_for_testing(flash.address, 10**12 * 10**18)
    pool = vpool_interface.at(factory.markets(0).virtual_pool)
    for a in accounts + [admin]:
        with boa.env.prank(a):
            stablecoin.approve(pool.address, 2**256 - 1)
            collateral_token.approve(pool.address, 2**256 - 1)
    return pool


@pytest.fixture
def policy(cryptopool, yb_lt, yb_amm, collateral_token, yb_allocated, seed_cryptopool, admin):
    """Market with an existing LevAMM book and the admission policy attached to its pool."""
    with boa.env.prank(admin):
        collateral_token._mint_for_testing(admin, 5 * 10**17)
        yb_lt.deposit(5 * 10**17, 5 * 10**17 * 100_000, 0)
        policy = boa.load('contracts/LTAdmissionPolicy.vy', cryptopool.address, yb_amm.address, RATIO_TOLERANCE)
        cryptopool.set_policy_contract(policy.address)
    return policy


def proportional_debt(cryptopool, yb_amm, assets):
    """Debt which keeps the LevAMM debt/collateral ratio: debt = lp_minted(debt, assets) * D / C."""
    collateral = yb_amm.collateral_amount()
    amm_debt = yb_amm.get_debt()

    def f(debt):
        return cryptopool.calc_token_amount([debt, assets], True) * amm_debt // collateral

    b0 = assets * cryptopool.balances(0) // cryptopool.balances(1)
    b1 = f(b0)
    for _ in range(8):
        g0, g1 = b1 - b0, f(b1) - b1
        if g1 == 0 or g1 == g0:
            break
        b0, b1 = b1, b1 - g1 * (b1 - b0) // (g1 - g0)
    return b1


def test_deposit_must_keep_ratio(policy, cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts):
    user = accounts[0]
    assets = 10**17
    collateral_token._mint_for_testing(user, 10 * assets)
    stablecoin._mint_for_testing(user, 10**24)
    debt = proportional_debt(cryptopool, yb_amm, assets)

    with boa.env.prank(user):
        # Any other debt is a fee-free LevAMM trade and is rejected, small or large
        for bad_debt in [0, debt * 99 // 100, debt * 101 // 100, debt * 9995 // 10000, debt * 10005 // 10000]:
            with boa.reverts("LT deviation"):
                yb_lt.deposit(assets, bad_debt, 0)

        ratio_before = yb_amm.get_debt() * 10**18 // yb_amm.collateral_amount()
        shares = yb_lt.deposit(assets, debt, 0)
        ratio_after = yb_amm.get_debt() * 10**18 // yb_amm.collateral_amount()
        assert abs(ratio_after - ratio_before) * 10**18 <= ratio_before * RATIO_TOLERANCE

        # Leaving is not gated, and a proportional round trip gives back no more than was put in
        asset_out, stables_out = yb_lt.emergency_withdraw(shares // 2)
        asset_out += yb_lt.withdraw(shares - shares // 2, 0)
        assert 0 < asset_out + stables_out // 100_000 <= assets
        ratio_exit = yb_amm.get_debt() * 10**18 // yb_amm.collateral_amount()
        assert abs(ratio_exit - ratio_before) * 10**18 <= ratio_before * RATIO_TOLERANCE


def test_virtual_pool_works(policy, virtual_pool, cryptopool, yb_amm, collateral_token, stablecoin, accounts):
    user = accounts[0]
    for i, in_amount in [(0, 1000 * 10**18), (1, 10**16)]:
        with boa.env.anchor():
            j = 1 - i
            with boa.env.prank(user):
                [stablecoin, collateral_token][i]._mint_for_testing(user, in_amount)
                expected_out = virtual_pool.get_dy(i, j, in_amount)
                out_amount = virtual_pool.exchange(i, j, in_amount, 0)
            assert out_amount > 0
            assert abs(expected_out - out_amount) / expected_out < 3e-5
            # The pool add made by VirtualPool is not an LT deposit; caches follow the pool
            assert policy.cached_supply() == cryptopool.user_supply()
            assert policy.cached_stable() == cryptopool.balances(0)
            assert cryptopool.balanceOf(yb_amm.address) == yb_amm.collateral_amount()


def test_amm_exchange_works(policy, cryptopool, yb_amm, stablecoin, accounts):
    user = accounts[0]
    stablecoin._mint_for_testing(user, 1000 * 10**18)
    with boa.env.prank(user):
        lp_out = yb_amm.exchange(0, 1, 1000 * 10**18, 0)
        assert lp_out > 0 and cryptopool.balanceOf(user) == lp_out
        stables_out = yb_amm.exchange(1, 0, lp_out, 0)
        assert 0 < stables_out < 1000 * 10**18
