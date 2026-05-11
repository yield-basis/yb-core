"""
Property test for the wash-trade oracle-shift attack on LEVAMM.

Scenario:
  1. Attacker trades down in LEVAMM (sells cryptopool-LP into AMM at oracle p_o_before)
  2. Attacker wash-trades in the cryptopool — round-trips inflate virtual_price,
     which raises CryptopoolLPOracle.price() (= 2 * virtual_price * sqrt(price_scale) * AGG / 1e18)
  3. Attacker trades back up in LEVAMM at the new higher p_o

Also tested in the reverse direction (buy LP first, wash, sell LP back), which is the
more suspicious direction since the attacker would be selling at the higher post-wash price.

Property:
  LT pricePerShare must not decrease across the sequence. The wash trade itself
  raises pps (donation accrues to all cryptopool LPs, including the LT's AMM
  collateral); the question is whether the surrounding AMM trades exploit the
  oracle shift to drain it back below the start.

State isolation: boa's pytest plugin (boa/test/plugin.py) anchors each fixture
setup and monkey-patches hypothesis to anchor every example. So the deterministic
setup below runs once per test (held by the fixture's anchor); each hypothesis
example mutates state inside its own anchor and unwinds back to fixture state.
"""
import boa
import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from types import SimpleNamespace


TEST_DEPOSIT = 10**18
PPS_REL_TOL = 1e-12


@pytest.fixture(scope="function")
def attack_setup(
    cryptopool, yb_lt, yb_amm, cryptopool_oracle,
    collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool,
):
    """One-shot deterministic setup. Also snapshots all pre-attack reads
    (pps, p_o, attacker LP balance, AMM debt) so every hypothesis example
    can skip re-deriving them from the unchanged fixture state."""
    attacker = accounts[1]
    p = cryptopool.price_oracle()

    collateral_token._mint_for_testing(admin, TEST_DEPOSIT)
    with boa.env.prank(admin):
        yb_lt.deposit(TEST_DEPOSIT, p * TEST_DEPOSIT // 10**18, 0)
        yb_lt.set_rate(0)

    collateral_token._mint_for_testing(attacker, 100 * 10**18)
    stablecoin._mint_for_testing(attacker, 100 * 100_000 * 10**18)
    with boa.env.prank(attacker):
        cryptopool.add_liquidity([10 * 100_000 * 10**18, 10 * 10**18], 0)

    return SimpleNamespace(
        attacker=attacker,
        p=p,
        pps_before=yb_lt.pricePerShare(),
        p_o_before=cryptopool_oracle.price(),
        attacker_lp=cryptopool.balanceOf(attacker),
        initial_debt=yb_amm.get_debt(),
    )


@given(
    sell_amount=st.integers(min_value=10**12, max_value=10**19),
    wash_iters=st.integers(min_value=0, max_value=20),
    wash_amount=st.integers(min_value=10**18, max_value=10**22),
    reverse=st.booleans(),
)
@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_oracle_shift_pps(
    attack_setup, cryptopool, yb_lt, yb_amm, cryptopool_oracle,
    sell_amount, wash_iters, wash_amount, reverse,
):
    s = attack_setup

    with boa.env.prank(s.attacker):
        if not reverse:
            # 1) Sell LP into AMM
            sell_lp = min(sell_amount, s.attacker_lp)
            if sell_lp == 0:
                return
            try:
                stable_out = yb_amm.exchange(1, 0, sell_lp, 0)
            except Exception as e:
                if any(x in str(e) for x in ("Unsafe", "Bad final state", "Empty AMM")):
                    return
                raise

            # 2) Wash-trade in cryptopool: 0->1->0 round trip per iteration
            for _ in range(wash_iters):
                try:
                    got = cryptopool.exchange(0, 1, wash_amount, 0)
                    cryptopool.exchange(1, 0, got, 0)
                except Exception:
                    break

            # 3) Buy LP back from AMM using the stable received
            buy_in = min(stable_out, yb_amm.get_debt())
            if buy_in > 0:
                try:
                    yb_amm.exchange(0, 1, buy_in, 0)
                except Exception as e:
                    if not any(x in str(e) for x in
                               ("Unsafe", "Bad final state", "Slippage", "Amount too large")):
                        raise
        else:
            # 1) Buy LP from AMM first (AMM debt is fixture-fresh on every example)
            stable_in = min(sell_amount * s.p // 10**18, s.initial_debt)
            if stable_in == 0:
                return
            try:
                lp_out = yb_amm.exchange(0, 1, stable_in, 0)
            except Exception as e:
                if any(x in str(e) for x in
                       ("Unsafe", "Bad final state", "Empty AMM", "Amount too large")):
                    return
                raise

            # 2) Wash-trade
            for _ in range(wash_iters):
                try:
                    got = cryptopool.exchange(0, 1, wash_amount, 0)
                    cryptopool.exchange(1, 0, got, 0)
                except Exception:
                    break

            # 3) Sell LP back into AMM
            if lp_out > 0:
                try:
                    yb_amm.exchange(1, 0, lp_out, 0)
                except Exception as e:
                    if not any(x in str(e) for x in
                               ("Unsafe", "Bad final state", "Slippage")):
                        raise

    pps_after = yb_lt.pricePerShare()
    p_o_after = cryptopool_oracle.price()
    assert pps_after - s.pps_before >= -PPS_REL_TOL * max(s.pps_before, pps_after), (
        f"PPS dropped: {s.pps_before} -> {pps_after}; "
        f"p_o: {s.p_o_before} -> {p_o_after}; "
        f"reverse={reverse} sell={sell_amount} wash_iters={wash_iters} wash_amt={wash_amount}"
    )
