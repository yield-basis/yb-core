import boa


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
