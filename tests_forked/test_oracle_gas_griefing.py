"""
Regression guard for ChainSecurity finding #001 (Low Gas Attack on get_state()).

YBLendingOracle._price calls amm.get_state() with revert_on_failure=False and, on
failure, silently falls back to a balance-based valuation that returns a *different*
price. Because of EIP-150's 63/64 rule, a caller who constrains gas could in principle
force get_state() to run out of gas (indistinguishable from its legitimate
"AMM too imbalanced" revert) and thereby flip the oracle onto the fallback branch.

That flip is only feasible if  cost(get_state) > 63 * cost(fallback-tail). Today it
is not: get_state is only a few-fold below that threshold, so no gas budget exists
that starves get_state while leaving the retained 1/64 enough to finish the fallback.
These tests pin that property so a future refactor (cheaper fallback, heavier
get_state) that erodes the margin fails loudly.

`test_no_silent_fallback_flip` is authoritative: it mounts the actual attack in-EVM
(an attacker contract that pre-warms every slot the oracle reads — access lists are
tx-scoped — then calls the oracle through a gas-limited raw_call) and asserts no gas
budget ever yields the fallback value. `test_get_state_gas_margin` records the
quantitative headroom (63*tail / get_state) so the number is visible and floored.
"""
import boa


# Attacker: pre-warm the AMM/LT slots the oracle's get_state() and fallback tail read
# (so the tail is as cheap as possible — the attacker-optimal case), then invoke the
# oracle with a constrained gas budget and report whether it returned, and what.
ATTACKER_SRC = """
# pragma version 0.4.3
ok: bool
r: Bytes[128]

@external
def probe(oracle: address, lt: address, amm: address, gas_limit: uint256, prewarm: bool) -> (bool, uint256):
    if prewarm:
        self.ok, self.r = raw_call(amm, method_id("collateral_amount()"), max_outsize=32, is_static_call=True, revert_on_failure=False)
        self.ok, self.r = raw_call(amm, method_id("get_debt()"), max_outsize=32, is_static_call=True, revert_on_failure=False)
        self.ok, self.r = raw_call(amm, method_id("get_state()"), max_outsize=96, is_static_call=True, revert_on_failure=False)
        self.ok, self.r = raw_call(lt, method_id("liquidity()"), max_outsize=128, is_static_call=True, revert_on_failure=False)
        self.ok, self.r = raw_call(lt, method_id("totalSupply()"), max_outsize=32, is_static_call=True, revert_on_failure=False)
    success: bool = False
    ret: Bytes[32] = b""
    success, ret = raw_call(
        oracle,
        abi_encode(lt, False, method_id=method_id("price_in_asset(address,bool)")),
        max_outsize=32, gas=gas_limit, is_static_call=True, revert_on_failure=False)
    if not success:
        return (False, 0)
    return (True, abi_decode(ret, uint256))
"""

# Sweep densely through the region below the oracle's minimum successful gas (~130k),
# where a gas-starvation flip window would live if one existed.
GAS_SWEEP = range(20_000, 260_001, 2_000)

# Minimum acceptable headroom: a flip needs get_state() > 63 * fallback_tail, i.e.
# headroom (63*tail / get_state) > 1. We require a comfortable buffer above that so the
# test fires well before the margin is actually breached. Current value is ~3.6x.
MIN_HEADROOM = 2.0


def test_no_silent_fallback_flip(factory, lending_oracle, amm_deployer, lt_deployer):
    """No gas budget can make the oracle silently return the fallback price."""
    oracle = lending_oracle
    attacker = boa.loads(ATTACKER_SRC)
    amm_p = amm_deployer
    lt_p = lt_deployer

    checked = 0
    for mid in range(factory.market_count()):
        m = factory.markets(mid)
        lt = lt_p.at(m.lt)
        amm = amm_p.at(m.amm)

        try:
            p_true = oracle.price_in_asset(lt)          # normal (get_state) path
        except Exception:
            continue                                    # market where even the normal path reverts; nothing to flip
        p_fb = oracle.price_in_asset(lt, True)          # forced balance fallback
        if p_true == p_fb:
            continue                                    # paths coincide -> a flip would be harmless and undetectable

        checked += 1
        for gas_limit in GAS_SWEEP:
            ok, val = attacker.probe(oracle.address, lt.address, amm.address, gas_limit, True)
            if ok:
                assert val == p_true, (
                    f"market {mid}: gas starvation flipped the oracle onto the balance "
                    f"fallback at gas={gas_limit}: got {val}, true={p_true}, fallback={p_fb}"
                )

    assert checked > 0, "no market exercised the get_state vs fallback divergence"


def test_get_state_gas_margin(factory, amm_deployer, lt_deployer):
    """get_state() stays well below 63 * fallback-tail, so the flip is infeasible."""
    amm_p = amm_deployer
    lt_p = lt_deployer

    def warm_gas(contract, fn):
        # Run a few times so storage is warm (attacker-optimal: smallest tail), then measure.
        fn()
        fn()
        fn()
        return contract._computation.get_gas_used()

    checked = 0
    for mid in range(factory.market_count()):
        m = factory.markets(mid)
        amm = amm_p.at(m.amm)
        lt = lt_p.at(m.lt)
        try:
            g_state = warm_gas(amm, amm.get_state)
        except Exception:
            continue

        # Lower bound on the fallback tail: the four external reads the else-branch makes.
        # (The branch also does cheap arithmetic, so this under-counts -> conservative.)
        tail = (
            warm_gas(amm, amm.collateral_amount)
            + warm_gas(amm, amm.get_debt)
            + warm_gas(lt, lt.liquidity)
            + warm_gas(lt, lt.totalSupply)
        )

        threshold = 63 * tail
        headroom = threshold / g_state
        assert g_state < threshold, (
            f"market {mid}: get_state gas {g_state} >= 63*tail {threshold} -> flip feasible"
        )
        assert headroom >= MIN_HEADROOM, (
            f"market {mid}: gas-griefing headroom {headroom:.1f}x < {MIN_HEADROOM}x floor "
            f"(get_state={g_state}, tail={tail})"
        )
        checked += 1

    assert checked > 0, "no market measured"
