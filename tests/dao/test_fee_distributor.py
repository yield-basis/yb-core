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
