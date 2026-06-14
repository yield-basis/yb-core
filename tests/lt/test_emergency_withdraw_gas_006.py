"""
Gas analysis for ChainSecurity #006 (Low Gas Attack on value_oracle() Call).

emergency_withdraw() reads value_oracle() via raw_call(revert_on_failure=False) and, on
failure, takes the killed-style proportional path (skipping _calculate_values). Under
EIP-150's 63/64 rule a caller could try to gas-starve value_oracle() into OOG to force
that path on a live AMM. The flip is only feasible if

    cost(value_oracle) > 63 * cost(rest-of-emergency_withdraw)

because the calling frame keeps only 1/64 of the gas-at-call and the rest of
emergency_withdraw must complete within it. Unlike #001 (where the fallback tail was a
few cheap staticcalls), here the "rest" is the entire withdrawal machinery
(_withdraw, cryptopool.remove_liquidity, several token transfers, _burn), which is far
more expensive than value_oracle() -- so the margin is enormous.
"""
import boa


def _measure_view(contract, fn):
    fn()
    fn()
    fn()
    return contract._computation.get_gas_used()


def test_emergency_withdraw_gas_margin(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool,
):
    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)

    # Open a position so admin holds LT shares.
    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, p * 10**18 // 10**18, 0)

    # 1) value_oracle() cost (the call an attacker would try to OOG). On the deployed
    #    mainnet AMMs this measures ~36.5k warm (heavier pool/debt state); it is cheaper
    #    in this minimal harness. Either way it is far below 63 * rest.
    g_value_oracle = _measure_view(yb_amm, yb_amm.value_oracle)

    # 2) full emergency_withdraw() cost (includes the swallowed value_oracle + the whole
    #    withdrawal machinery). The "rest" the attacker must fit into 1/64 is this minus
    #    value_oracle (and the proportional path is even cheaper than the measured live path).
    shares = yb_lt.balanceOf(admin)
    stablecoin._mint_for_testing(admin, 10**24)   # to cover any debt shortfall on withdraw
    with boa.env.prank(admin):
        yb_lt.emergency_withdraw(shares // 2)
    g_emergency = yb_lt._computation.get_gas_used()

    rest = g_emergency - g_value_oracle           # conservative proxy for the post-call tail
    # Feasibility threshold for the flip: value_oracle > 63 * rest. Report how far we are.
    headroom = 63 * rest / g_value_oracle         # >> 1 means the attack is infeasible

    print(f"\nvalue_oracle gas      = {g_value_oracle}")
    print(f"emergency_withdraw gas = {g_emergency}")
    print(f"rest (post-call tail)  = {rest}")
    print(f"flip needs value_oracle > 63*rest = {63*rest}  (value_oracle is {g_value_oracle})")
    print(f"infeasibility margin 63*rest/value_oracle = {headroom:.0f}x")

    # The withdrawal tail dwarfs value_oracle, so the 63/64 flip is wildly infeasible.
    assert g_emergency > g_value_oracle
    assert 63 * rest > 50 * g_value_oracle, "margin unexpectedly small -- re-examine #006"
