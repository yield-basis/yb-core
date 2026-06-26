import boa
import pytest
from tests_forked.networks import NETWORK

# Confirms, at the block this test was committed (see FORK_BLOCK below), that
# the LTMigrator swap fixes the reported "Access" failure: the OLD migrator
# (0xE707...) still reverts, while the redeployed NEW migrator (0x3e6D...) lets
# the user migrate end-to-end. The fork is pinned because these assertions read
# live on-chain state that has since moved on past "latest".
#
# Background (the bug the redeploy fixes):
#   The OLD LTMigrator was deployed wired to a STALE HybridFactoryOwner
#   (0x0f4e...), but the Factory's admin has since been migrated by the DAO to
#   the NEW/fixed HybridFactoryOwner (0xb8BA...).
#
#   migrate_plain() -> _migrate_plain() reaches LTMigrator.vy:198
#       extcall FACTORY_OWNER.lt_allocate_stablecoins(lt_from, 0)
#   which routes through the OLD owner (0x0f4e...). The old owner still has
#   disabled_lts[lt_from] == True, so it takes the deallocate branch and calls
#       lt_from.allocate_stablecoins(available_limit)
#   LT._check_admin (LT.vy) accepts only the LT's admin (the Factory) or the
#   *current* Factory.admin() (the NEW owner 0xb8BA...). The caller is the OLD
#   owner 0x0f4e..., so the LT reverts with "Access".
#
#   FACTORY_OWNER is immutable, so the migrator cannot be repaired in place. It
#   was redeployed against the current owner (0x3e6D...) and the swap was
#   enacted via the DAO vote in
#   scripts/voting/create_vote_redeploy_migrator_owner.py.

OLD_MIGRATOR = "0xE707c7a9dD58fb7eEa17acFF875CEF8d10eD1a9F"  # stale-owner migrator
NEW_MIGRATOR = "0x3e6Db519752d4d1EEEd0539A5F7BCF3Aa4089b62"  # redeployed migrator
LT_FROM = "0xfBF3C16676055776Ab9B286492D8f13e30e2E763"
LT_TO = "0x651D4b8168488FA163D85304662E8278d4c55BAa"
USER = "0xD24C29f58fA7F57fb70EBb059B1ffd795E23800e"
FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"

STALE_OWNER = "0x0f4e1a1BcDe549653E9302Ba1cAaB403373f1048"  # old migrator's owner
CURRENT_OWNER = "0xb8BA33CD1Ccb091a8468572950bD3669723FA5C6"  # live Factory.admin()
OWNER_ADMIN = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"  # DAO Ownership agent


# Block mined at this test's commit time (05c250e, 2026-06-04 18:09:49 UTC).
# These tests assert live on-chain state that was only true when committed
# (e.g. the reported user still holds lt_from, the migration not yet done), so
# we pin the fork to that block instead of a moving "latest" head.
FORK_BLOCK = 25245721


@pytest.fixture(scope="module", autouse=True)
def forked_env():
    # Fork at the commit-time block so on-chain state matches the assertions.
    with boa.fork(NETWORK, block_identifier=FORK_BLOCK):
        yield


@pytest.fixture(scope="module")
def factory():
    return boa.load_partial("contracts/Factory.vy").at(FACTORY)


@pytest.fixture(scope="module")
def owner(factory):
    return boa.load_partial("contracts/HybridFactoryOwner.vy").at(factory.admin())


def test_old_migrator_wired_to_stale_owner(factory):
    """The OLD migrator's immutable owner is not the current Factory admin."""
    old = boa.load_partial("contracts/LTMigrator.vy").at(OLD_MIGRATOR)
    assert old.FACTORY_OWNER().lower() == STALE_OWNER.lower()
    assert factory.admin().lower() == CURRENT_OWNER.lower()
    # This mismatch is the whole problem.
    assert old.FACTORY_OWNER().lower() != factory.admin().lower()


def test_new_migrator_wired_to_current_owner(factory):
    """The NEW migrator is wired to the live Factory admin."""
    new = boa.load_partial("contracts/LTMigrator.vy").at(NEW_MIGRATOR)
    assert new.FACTORY_OWNER().lower() == factory.admin().lower()
    assert new.FACTORY_OWNER().lower() == CURRENT_OWNER.lower()


def test_old_migrator_reverts_with_access():
    """User migration through the OLD migrator reverts 'Access'."""
    old = boa.load_partial("contracts/LTMigrator.vy").at(OLD_MIGRATOR)
    lt_from = boa.load_partial("contracts/LT.vy").at(LT_FROM)

    shares = lt_from.balanceOf(USER)
    assert shares > 0, "user has no lt_from to migrate"

    with boa.env.prank(USER):
        lt_from.approve(OLD_MIGRATOR, 2**256 - 1)
        # Reverts inside lt_from.allocate_stablecoins (LT._check_admin) because
        # the routed owner (0x0f4e...) is no longer the Factory admin.
        with boa.reverts("Access"):
            old.migrate_plain(LT_FROM, LT_TO, shares, 0)


def test_new_migrator_succeeds(owner):
    """
    The redeployed migrator (0x3e6D...) lets the reported user migrate
    end-to-end.

    The new migrator must be a registered limit setter on the current owner so
    the owner accepts its non-zero lt_allocate_stablecoins calls (the lt_to
    allocation step). set_limit_setter is ADMIN-only, and that ADMIN is the DAO
    Ownership agent -- so registering it REQUIRED A DAO VOTE
    (scripts/voting/create_vote_redeploy_migrator_owner.py).

    At FORK_BLOCK the vote may or may not have executed yet, so we apply the
    approval here if (and only if) the new migrator is not already a registered
    limit setter, by pranking the owner ADMIN to simulate the executed vote.
    """
    new = boa.load_partial("contracts/LTMigrator.vy").at(NEW_MIGRATOR)
    lt_from = boa.load_partial("contracts/LT.vy").at(LT_FROM)
    lt_to = boa.load_partial("contracts/LT.vy").at(LT_TO)

    # The approval (DAO-vote-gated set_limit_setter). Already in place at head;
    # applied here only if the fork predates the vote's execution.
    if not owner.limit_setters(NEW_MIGRATOR):
        assert owner.ADMIN().lower() == OWNER_ADMIN.lower()
        with boa.env.prank(owner.ADMIN()):
            owner.set_limit_setter(NEW_MIGRATOR, True)
    assert owner.limit_setters(NEW_MIGRATOR) is True

    shares = lt_from.balanceOf(USER)
    assert shares > 0

    to_before = lt_to.balanceOf(USER)
    preview = new.preview_migrate_plain(LT_FROM, LT_TO, shares, 10**18)

    with boa.env.prank(USER):
        lt_from.approve(NEW_MIGRATOR, 2**256 - 1)
        new.migrate_plain(LT_FROM, LT_TO, shares, 0)

    to_after = lt_to.balanceOf(USER)
    minted = to_after - to_before

    # Position moved: lt_from withdrawn, lt_to shares received.
    assert lt_from.balanceOf(USER) == 0
    assert minted > 0
    print("preview:", preview, "minted:", minted)
