import boa
import pytest


@pytest.fixture(scope="module")
def checker():
    return boa.load('contracts/dao/CallComparator.vy')


@pytest.fixture(scope="module")
def token(token_mock):
    return token_mock.deploy('Test', 'TST', 18)


def method_id(sig):
    return boa.eval(f'method_id("{sig}")')


def test_check_equal_passes(checker, token):
    selector = method_id("totalSupply()")
    checker.check_equal(token.address, selector, 0)


def test_check_equal_reverts(checker, token):
    token._mint_for_testing(token.address, 10**18)
    selector = method_id("totalSupply()")
    with boa.reverts("Not equal"):
        checker.check_equal(token.address, selector, 0)


def test_check_nonequal_passes(checker, token):
    token._mint_for_testing(token.address, 10**18)
    selector = method_id("totalSupply()")
    checker.check_nonequal(token.address, selector, 0)


def test_check_nonequal_reverts(checker, token):
    selector = method_id("totalSupply()")
    supply = token.totalSupply()
    with boa.reverts("Equal"):
        checker.check_nonequal(token.address, selector, supply)


def test_check_gt_passes(checker, token):
    token._mint_for_testing(token.address, 10**18)
    selector = method_id("totalSupply()")
    checker.check_gt(token.address, selector, 10**18 - 1)


def test_check_gt_reverts(checker, token):
    selector = method_id("totalSupply()")
    with boa.reverts("Not greater"):
        checker.check_gt(token.address, selector, 0)


def test_check_lt_passes(checker, token):
    selector = method_id("totalSupply()")
    checker.check_lt(token.address, selector, 1)


def test_check_lt_reverts(checker, token):
    token._mint_for_testing(token.address, 10**18)
    selector = method_id("totalSupply()")
    with boa.reverts("Not less"):
        checker.check_lt(token.address, selector, 10**18)


def test_check_timestamp_gt_passes(checker):
    checker.check_timestamp_gt(0)


def test_check_timestamp_gt_reverts(checker):
    with boa.reverts("Timestamp not greater"):
        checker.check_timestamp_gt(2**256 - 1)


def test_check_timestamp_lt_passes(checker):
    checker.check_timestamp_lt(2**256 - 1)


def test_check_timestamp_lt_reverts(checker):
    with boa.reverts("Timestamp not less"):
        checker.check_timestamp_lt(0)
