import boa
import pytest
from eth_utils import to_checksum_address
from scripts.distribution import distribute_season2
from scripts.distribution.distribute_season2 import (
    DEPLOYER,
    SEASON_1_MULTISEND,
    YB,
    YB_HOLDER,
    check_multisend,
    distribute,
    load_allocation,
)
from tests_forked.networks import NETWORK


FORK_BLOCK = 26351000  # November 2026
WALLETS = 250  # three batches
SEASON_1_WALLET = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # first entry of split-nonvested.json


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    with boa.fork(NETWORK, block_identifier=FORK_BLOCK):
        boa.env.eoa = DEPLOYER
        yield


@pytest.fixture(scope="module")
def yb(forked_env):
    return boa.load_partial("contracts/dao/YB.vy").at(YB)


@pytest.fixture(scope="module")
def amounts():
    # Fresh addresses, each owed a different amount
    return {to_checksum_address(f"0x{i + 1:040x}" .replace("0x00", "0xfe", 1)): (i + 1) * 10**18 + i for i in range(WALLETS)}


@pytest.fixture
def multisend(forked_env):
    return boa.load("contracts/dao/Multisend.vy", YB)


@pytest.fixture
def funded(yb, amounts):
    """ADMIN holding exactly what the wallets are owed; undone after each test."""
    with boa.env.anchor():
        with boa.env.prank(YB_HOLDER):
            yb.transfer(DEPLOYER, sum(amounts.values()))
        yield


def write_csv(path, rows):
    path.write_text("user,yb,fraction\n" + "".join(f"{user},{yb},0.5\n" for user, yb in rows))
    return str(path)


def test_holder_can_fund_the_whole_distribution(yb):
    assert yb.balanceOf(YB_HOLDER) >= 1_406_250 * 10**18


def test_season_1_multisend_skips_past_recipients(yb, funded):
    season_1 = boa.load_partial("contracts/dao/Multisend.vy").at(SEASON_1_MULTISEND)
    assert season_1.already_sent(SEASON_1_WALLET)
    before = yb.balanceOf(SEASON_1_WALLET)
    season_1.send([SEASON_1_WALLET], [10**18])  # no revert, and nothing moves
    assert yb.balanceOf(SEASON_1_WALLET) == before
    with pytest.raises(AssertionError, match="Season 1"):
        check_multisend(season_1, DEPLOYER)


def test_sends_exact_amounts(yb, amounts, multisend, funded):
    assert distribute(multisend, yb, DEPLOYER, amounts) == sum(amounts.values())
    assert all(yb.balanceOf(user) == amount for user, amount in amounts.items())
    assert yb.balanceOf(DEPLOYER) == 0
    assert yb.allowance(DEPLOYER, multisend.address) == 0


def test_second_run_sends_nothing(yb, amounts, multisend, funded):
    distribute(multisend, yb, DEPLOYER, amounts)
    assert distribute(multisend, yb, DEPLOYER, amounts) == 0
    assert all(yb.balanceOf(user) == amount for user, amount in amounts.items())


def test_resumes_after_a_partial_run(yb, amounts, multisend, funded):
    items = list(amounts.items())
    done = dict(items[:130])
    yb.approve(multisend.address, sum(done.values()))
    multisend.send(list(done)[:100], list(done.values())[:100])
    multisend.send(list(done)[100:], list(done.values())[100:])

    assert distribute(multisend, yb, DEPLOYER, amounts) == sum(a for _, a in items[130:])
    assert all(yb.balanceOf(user) == amount for user, amount in amounts.items())
    assert yb.balanceOf(DEPLOYER) == 0


def test_stops_before_sending_when_underfunded(yb, amounts, multisend, funded):
    yb.transfer(YB_HOLDER, 1)
    with pytest.raises(AssertionError, match="ADMIN holds"):
        distribute(multisend, yb, DEPLOYER, amounts)
    assert not any(multisend.already_sent(user) for user in amounts)


def test_only_admin_can_send(amounts, multisend):
    with boa.env.prank(YB_HOLDER), boa.reverts():
        multisend.send(list(amounts)[:1], [1])


def test_live_send_needs_a_fixed_multisend():
    with pytest.raises(AssertionError, match="Set MULTISEND"):
        distribute_season2.send_live({}, "digest")


def test_live_send_needs_the_reviewed_csv(monkeypatch):
    monkeypatch.setattr(distribute_season2, "MULTISEND", YB)
    monkeypatch.setattr(distribute_season2, "CSV_SHA256", "reviewed")
    with pytest.raises(AssertionError, match="not the reviewed one"):
        distribute_season2.send_live({}, "changed")


def test_load_allocation_is_exact(tmp_path):
    a, b = "0x" + "aa" * 20, "0x" + "bb" * 20
    path = write_csv(tmp_path / "ok.csv", [(a, "0.1"), (b, "0.2000000000001")])
    amounts = load_allocation(path, total=3 * 10**17)
    assert sum(amounts.values()) == 3 * 10**17
    assert amounts[to_checksum_address(a)] == 10**17


def test_load_allocation_rejects_a_wrong_total(tmp_path):
    path = write_csv(tmp_path / "bad.csv", [("0x" + "aa" * 20, "0.1")])
    with pytest.raises(AssertionError, match="adds up to"):
        load_allocation(path, total=3 * 10**17)


def test_load_allocation_rejects_duplicates(tmp_path):
    a = "0x" + "aa" * 20
    path = write_csv(tmp_path / "dup.csv", [(a, "0.1"), (a.upper().replace("0X", "0x"), "0.2")])
    with pytest.raises(AssertionError, match="Duplicate"):
        load_allocation(path, total=3 * 10**17)
