import boa
import pytest


RESERVE = 10**9
RATE = 10**9 // (365 * 86400)


@pytest.fixture(scope="session")
def yb(admin):
    with boa.env.prank(admin):
        return boa.load('contracts/dao/YB.vy', RESERVE, RATE)
