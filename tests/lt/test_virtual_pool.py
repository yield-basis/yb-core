import boa
import pytest


@pytest.fixture(scope="session")
def virtual_pool(factory, flash, stablecoin, collateral_token, admin, accounts):
    stablecoin._mint_for_testing(flash.address, 10**12 * 10**18)
    vp_impl = boa.load_partial('contracts/VirtualPool.vy')
    pool = vp_impl.at(factory.markets(0).virtual_pool)
    for a in accounts + [admin]:
        with boa.env.prank(a):
            stablecoin.approve(pool.address, 2**256 - 1)
            collateral_token.approve(pool.address, 2**256 - 1)
    return pool


def test_virtual_pool(factory, cryptopool, yb_lt, collateral_token, stablecoin, yb_allocated,
                      seed_cryptopool, virtual_pool, accounts, admin):
    for i, in_amount in [
        (0, 10**18),
        (0, 1000 * 10**18),
        (0, 10_000 * 10**18),
        (1, 10**14),
        (1, 10**16),
        (1, 3 * 10**16)
    ]:
        with boa.env.anchor():
            j = 1 - i
            user = accounts[0]

            with boa.env.prank(admin):
                collateral_token._mint_for_testing(admin, 5 * 10**17)
                yb_lt.deposit(5 * 10**17, 5 * 10**17 * 100_000, 0)

            with boa.env.prank(user):
                if i == 0:
                    stablecoin._mint_for_testing(user, in_amount)
                    discount = 1e-7
                else:
                    collateral_token._mint_for_testing(user, in_amount)
                    discount = 2.5e-5  # due to NOISE_FEE=1e-5 in cryptopool (ugh)

                expected_out = virtual_pool.get_dy(i, j, in_amount)

                before = [stablecoin.balanceOf(user), collateral_token.balanceOf(user), cryptopool.balanceOf(user)]

                with boa.reverts():
                    virtual_pool.exchange(i, j, in_amount, int(expected_out * (1 + discount)))
                out_amount = virtual_pool.exchange(i, j, in_amount, int(expected_out * (1 - discount)))

                after = [stablecoin.balanceOf(user), collateral_token.balanceOf(user), cryptopool.balanceOf(user)]

                assert before[i] - after[i] == in_amount
                assert after[j] - before[j] == out_amount
                assert before[2] == after[2] == 0
