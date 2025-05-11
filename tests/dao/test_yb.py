import boa


def test_mint(yb, admin, accounts):
    assert yb.totalSupply() == 0
    with boa.reverts():
        with boa.env.prank(accounts[1]):
            yb.mint(accounts[0], 10**18)

    with boa.env.prank(admin):
        yb.mint(accounts[0], 10**18)
        assert yb.totalSupply() == 10**18
        assert yb.balanceOf(accounts[0]) == 10**18

        yb.renounce_ownership()
        assert yb.is_minter(admin) == False

        with boa.reverts():
            yb.mint(accounts[0], 10**17)
