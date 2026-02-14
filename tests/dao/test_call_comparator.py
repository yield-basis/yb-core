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


def test_check_called_after_first_call(checker):
    """First call should always succeed since last_called is 0"""
    checker.check_called_after(0)


def test_check_called_after_too_early(checker):
    """Second call within the delay should revert"""
    with boa.env.prank(boa.env.generate_address()):
        checker.check_called_after(0)
        with boa.reverts("Too early"):
            checker.check_called_after(3600)


def test_check_called_after_sufficient_delay(checker):
    """Second call after sufficient delay should succeed"""
    sender = boa.env.generate_address()
    with boa.env.prank(sender):
        checker.check_called_after(0)
        boa.env.time_travel(seconds=3600)
        checker.check_called_after(3600)


def test_check_called_after_updates_timestamp(checker):
    """Each successful call should update last_called"""
    sender = boa.env.generate_address()
    with boa.env.prank(sender):
        checker.check_called_after(0)
        t1 = checker.last_called(sender)
        boa.env.time_travel(seconds=100)
        checker.check_called_after(100)
        t2 = checker.last_called(sender)
        assert t2 > t1


def test_check_called_after_independent_per_sender(checker):
    """Different senders should have independent last_called timestamps"""
    sender1 = boa.env.generate_address()
    sender2 = boa.env.generate_address()
    with boa.env.prank(sender1):
        checker.check_called_after(0)
    boa.env.time_travel(seconds=100)
    # sender2 has never called, so should succeed even with large delay
    with boa.env.prank(sender2):
        checker.check_called_after(10**9)
    # sender1 should still be blocked for a large delay
    with boa.env.prank(sender1):
        with boa.reverts("Too early"):
            checker.check_called_after(10**9)
