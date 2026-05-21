import boa


def test_multisend_pays_each_recipient(admin, accounts, token_mock):
    token = token_mock.deploy('T', 'T', 18)
    with boa.env.prank(admin):
        ms = boa.load('contracts/dao/Multisend.vy', token.address)
    users = accounts[0:3]
    amounts = [10**18, 2 * 10**18, 3 * 10**18]
    with boa.env.prank(admin):
        token._mint_for_testing(admin, 100 * 10**18)
        token.approve(ms.address, 2**256 - 1)
        ms.send(users, amounts)
    assert token.balanceOf(users[0]) == amounts[0]
    assert token.balanceOf(users[1]) == amounts[1]
    assert token.balanceOf(users[2]) == amounts[2]


def test_multisend_rejects_mismatched_lengths(admin, accounts, token_mock):
    token = token_mock.deploy('T', 'T', 18)
    with boa.env.prank(admin):
        ms = boa.load('contracts/dao/Multisend.vy', token.address)
    with boa.env.prank(admin):
        token._mint_for_testing(admin, 100 * 10**18)
        token.approve(ms.address, 2**256 - 1)
    with boa.reverts():
        with boa.env.prank(admin):
            ms.send([accounts[0], accounts[1]], [10**18])


def test_multisend_index_pairs_by_user_position(admin, accounts, token_mock):
    token = token_mock.deploy('T', 'T', 18)
    with boa.env.prank(admin):
        ms = boa.load('contracts/dao/Multisend.vy', token.address)
    a, b, c = accounts[0], accounts[1], accounts[2]
    with boa.env.prank(admin):
        token._mint_for_testing(admin, 10**30)
        token.approve(ms.address, 2**256 - 1)
        ms.send([a, b], [10**18, 10**18])
        ms.send([a, c], [10**36, 5 * 10**18])
    assert token.balanceOf(a) == 10**18
    assert token.balanceOf(c) == 5 * 10**18
