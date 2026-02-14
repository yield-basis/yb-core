import boa
import pytest


@pytest.fixture(scope="module")
def checker():
    return boa.load('contracts/dao/EqualityChecker.vy')


@pytest.fixture(scope="module")
def token(token_mock):
    return token_mock.deploy('Test', 'TST', 18)


def method_id(sig):
    return boa.eval(f'method_id("{sig}")')


def test_check_equal_passes(checker, token):
    selector = method_id("totalSupply()")
    checker.check_equal(token.address, selector, 0)


def test_check_equal_reverts(checker, token, admin):
    with boa.env.prank(admin):
        token._mint_for_testing(admin, 10**18)
    selector = method_id("totalSupply()")
    with boa.reverts("Not equal"):
        checker.check_equal(token.address, selector, 0)


def test_check_nonequal_passes(checker, token, admin):
    with boa.env.prank(admin):
        token._mint_for_testing(admin, 10**18)
    selector = method_id("totalSupply()")
    checker.check_nonequal(token.address, selector, 0)


def test_check_nonequal_reverts(checker, token):
    selector = method_id("totalSupply()")
    supply = token.totalSupply()
    with boa.reverts("Equal"):
        checker.check_nonequal(token.address, selector, supply)
