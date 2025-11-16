import boa
import pytest
from hypothesis import given, settings
import hypothesis.strategies as st


@pytest.fixture(scope="session")
def token_set(token_mock):
    decimals = [8] * 2 + [18] * 8
    return [token_mock.deploy("Token %s" % i, "TOK-%s" % i, decimals[i]) for i in range(10)]


@pytest.fixture(scope="session")
def fee_distributor(token_set, ve_yb, admin):
    initial_set = [token_set[0], token_set[1], token_set[5], token_set[9]]
    return boa.load('contracts/dao/FeeDistributor.vy', initial_set, ve_yb, admin)


@given(token_ids=st.lists(st.integers(min_value=0, max_value=9), min_size=0, max_size=9))
@settings(max_examples=50)
def test_add_set(fee_distributor, token_set, token_ids, admin):
    token_set = [token_set[i] for i in list(set(token_ids))]
    with boa.reverts():
        fee_distributor.add_token_set(token_set)
    with boa.env.prank(admin):
        fee_distributor.add_token_set(token_set)
    assert fee_distributor.current_token_set() == 2
    for i, token in enumerate(token_set):
        assert fee_distributor.token_sets(2, i) == token.address


def test_recover(fee_distributor, token_set, admin):
    amounts = [10**18 + i * 10**18 for i in range(len(token_set))]
    for token, amount in zip(token_set, amounts):
        token._mint_for_testing(fee_distributor.address, amount)

    for token, amount in zip(token_set, amounts):
        with boa.reverts():
            fee_distributor.recover_token(token.address, admin)
        with boa.env.prank(admin):
            fee_distributor.recover_token(token.address, admin)
            assert token.balanceOf(admin) == amount


@given(
        amounts=st.lists(st.integers(min_value=0, max_value=10**30), min_size=10, max_size=10),
        epoch_count=st.integers(min_value=1, max_value=51)
)
@settings(max_examples=50)
def test_claim_empty(fee_distributor, token_set, accounts, amounts, epoch_count):
    for token, amount in zip(token_set, amounts):
        token._mint_for_testing(fee_distributor.address, amount)
    fee_distributor.claim(accounts[0], epoch_count)


@given(
        amounts=st.lists(st.integers(min_value=0, max_value=10**30), min_size=4, max_size=4)
)
@settings(max_examples=500)
def test_claim_two_users(fee_distributor, token_set, accounts, admin, amounts, ve_yb, yb):
    used_set = [token_set[0], token_set[1], token_set[5], token_set[9]]
    users = accounts[:2]
    ve_amounts = [10**18, 3 * 10**18]
    lock_time = boa.env.evm.patch.timestamp + 4 * 365 * 86400

    for user, ve_amount in zip(users, ve_amounts):
        with boa.env.prank(admin):
            yb.mint(user, ve_amount)
        with boa.env.prank(user):
            yb.approve(ve_yb.address, 2**256 - 1)
            ve_yb.create_lock(ve_amount, lock_time)

    for token, amount in zip(used_set, amounts):
        token._mint_for_testing(fee_distributor.address, amount)

    fee_distributor.fill_epochs()

    # 5 weeks claims everything distributed
    boa.env.time_travel(5 * 7 * 86400)

    for user, ve_amount in zip(users, ve_amounts):
        with boa.env.prank(user):
            fee_distributor.claim()
        for token, amount in zip(used_set, amounts):
            user_has = token.balanceOf(user)
            user_expected = amount * ve_amount // sum(ve_amounts)
            assert abs(user_has - user_expected) <= max(user_expected * 1e-6, 8)

    for token in used_set:
        assert token.balanceOf(fee_distributor.address) <= 8
