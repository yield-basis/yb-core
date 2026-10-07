# pragma version 0.4.3
# pragma optimize gas
"""
@title LTAdmissionPolicy
@notice Twocrypto policy which only admits Yield Basis LT deposits that keep the
        LevAMM debt/collateral ratio. Fee and price_scale stay native.
@dev Template: a policy that also steers the fee or price_scale keeps the
     "LT deposit gate" parts below as they are and replaces the two stubs.

     WHY THE GATE EXISTS

     LT.deposit(assets, debt) lets the caller pick `debt`. The LevAMM book gains
     (added_lp, debt), shares are minted for the value added, and an exit takes
     the same fraction of collateral and of debt back out. If the pair was added
     in the ratio the book already had, the exit returns exactly what went in.
     If not, the book is left with a different collateral/debt mix at unchanged
     value, which is what LevAMM.exchange does, but without its fee. So the only
     deposit that is not a free trade against the existing holders is

         debt == added_lp * old_debt / old_collateral

     WHY A POOL POLICY CAN ENFORCE IT

     LT.deposit runs in this order:

         1. pool.add_liquidity([debt, assets], receiver = LevAMM)
              - pool ledgers grow by the two amounts
              - LP is minted to the LevAMM
              - the pool calls update_pool_state()          <-- we are here
         2. amm._deposit(lp, debt)   # only now the LevAMM book is updated

     At the callback the LevAMM still reports its old collateral and debt, while
     the pool already shows the deposit. A revert here unwinds the deposit.

     WHAT IS MEASURED, AND FROM WHERE

         added_lp   = growth of pool.user_supply()
         added_debt = growth of xp[0] + pool.admin_balances(0)
         old book   = amm.collateral_amount(), amm.get_debt()

     Every input is a ledger of the pool or of the LevAMM. None of them is a
     token balance or a price, so token gifts and price moves cannot shift them.

     WHAT IS NOT GATED

     Swaps, donations, withdrawals, LevAMM.exchange and VirtualPool trades. They
     are told apart from an LT deposit by where the new LP is (see step 3 in update_pool_state).
"""

interface Pool:
    def balanceOf(account: address) -> uint256: view
    def user_supply() -> uint256: view
    def admin_balances(i: uint256) -> uint256: view
    def precisions() -> uint256[2]: view
    def coins(i: uint256) -> address: view

interface LevAMM:
    def collateral_amount() -> uint256: view
    def get_debt() -> uint256: view
    def COLLATERAL() -> address: view
    def STABLECOIN() -> address: view


N_COINS: constant(uint256) = 2
PRECISION: constant(uint256) = 10**18

POOL: public(immutable(address))
AMM: public(immutable(address))
# Relative band around the proportional debt, 1e18 = 100%. It only has to absorb
# what moves between quoting and inclusion: debt interest (well under 0.01 bp per
# block) and trades by others (a $10k pool swap moved the proportional debt by up
# to ~1 bp on the BTC pools). A deposit outside the band reverts and is re-quoted.
RATIO_TOLERANCE: public(immutable(uint256))


# ------------------------------ LT deposit gate: state ------------------------------
# The pool as of the previous callback. Every operation that changes these values
# makes a callback, so at the next one they are the "before" side of the deltas.

# pool.user_supply(): LP supply without donation shares. Zero until attached.
cached_supply: public(uint256)
# xp[0]: the pool's stablecoin ledger (coin 0 has precision 1, so xp[0] == balances[0]).
cached_stable: public(uint256)
# pool.admin_balances(0): stablecoin set aside as pool admin fee.
cached_admin: public(uint256)
# LP which the LevAMM holds beyond its recorded collateral. Zero unless someone
# sent LP to the LevAMM directly; kept so that such a gift is not counted twice.
amm_surplus: public(uint256)


@deploy
def __init__(pool: address, amm: address, ratio_tolerance: uint256):
    """
    @param pool Twocrypto pool this policy is attached to
    @param amm LevAMM which holds the pool LP as collateral
    @param ratio_tolerance Relative band around the proportional debt
    """
    assert ratio_tolerance <= PRECISION // 1000, "ratio tolerance"
    # The gate compares the pool's ledgers with this LevAMM's book, so they must
    # belong to the same market: one LevAMM per pool, stablecoin is coin 0.
    assert staticcall LevAMM(amm).COLLATERAL() == pool, "amm collateral"
    assert staticcall LevAMM(amm).STABLECOIN() == staticcall Pool(pool).coins(0), "amm stablecoin"
    # xp[0] is read as a raw stablecoin amount.
    assert (staticcall Pool(pool).precisions())[0] == 1, "pool precision"
    POOL = pool
    AMM = amm
    RATIO_TOLERANCE = ratio_tolerance


# --------------------------- Fee and price_scale: native ----------------------------
# Replace these two to steer the pool. They are views, so the gate cannot live
# here: get_fee runs before the LP is minted and nothing can be stored from it.

@external
@pure
def get_fee(_xp: uint256[N_COINS]) -> uint256:
    """@notice Return 0 to use Twocrypto's native mid/out fee curve."""
    return 0


@external
@pure
def get_price_scale() -> uint256:
    """@notice Return 0 to use Twocrypto's native price_scale target."""
    return 0


# ---------------------------- LT deposit gate: the check ----------------------------

@external
def update_pool_state(
    xp: uint256[N_COINS],
    _price_scale: uint256,
    _price_oracle: uint256,
    _last_prices: uint256,
    _virtual_price: uint256,
    _xcp_profit: uint256,
    _D: uint256,
    _oracle_timestamp: uint256,
):
    """
    @notice Called by the pool at the end of every operation.
    @dev The callback of a balanced removal is best-effort: the pool carries on
         if it fails. A failure would leave the caches stale, so nothing on the
         removal path may revert. The pool gives that call 300k gas, far more
         than this function uses.
    """
    assert msg.sender == POOL, "auth!"

    # A policy with its own state (EMAs, snapshots) updates it here, before or
    # after the gate. The gate only needs xp[0].

    # ---- Step 1: refresh the caches and leave early for swaps and donations ----
    supply: uint256 = staticcall Pool(POOL).user_supply()
    old_supply: uint256 = self.cached_supply
    old_stable: uint256 = self.cached_stable
    self.cached_stable = xp[0]
    if supply == old_supply and old_supply != 0:
        # User LP supply is unchanged: a swap or a donation, never a deposit.
        # The admin ledger still has to stay current. Of these operations only a
        # swap paying out the stablecoin can add to it, and that always lowers
        # xp[0]. xp[1] is not used to classify anything: it is rescaled by
        # price_scale and moves on a rebalance without any balance change.
        if xp[0] < old_stable:
            self.cached_admin = staticcall Pool(POOL).admin_balances(0)
        return

    # ---- Step 2: user LP supply changed (or this is the attachment callback) ----
    self.cached_supply = supply
    old_admin: uint256 = self.cached_admin
    admin: uint256 = staticcall Pool(POOL).admin_balances(0)
    self.cached_admin = admin

    # LP the LevAMM holds versus LP it has booked as collateral. Equal at rest.
    # Clamped instead of asserted: this also runs for removals.
    lp_balance: uint256 = staticcall Pool(POOL).balanceOf(AMM)
    collateral: uint256 = staticcall LevAMM(AMM).collateral_amount()
    surplus: uint256 = lp_balance - min(lp_balance, collateral)
    old_surplus: uint256 = self.amm_surplus

    # old_supply == 0 is the first callback: record the state, check nothing.
    if supply > old_supply and old_supply != 0:
        added_lp: uint256 = supply - old_supply

        # ---- Step 3: is this add an LT deposit? ----
        # LT.deposit is the only path that mints LP to the LevAMM before the
        # LevAMM books it, so only then does the surplus grow by the minted
        # amount. Any other add (VirtualPool) mints to its caller and leaves the
        # surplus alone. The LevAMM never sends LP out by itself, so an entry
        # cannot make the surplus look smaller; an LP gift can only make another
        # add look like an entry (which then fails the check), never the reverse.
        if surplus >= old_surplus + added_lp:
            surplus -= added_lp

            # ---- Step 4: the debt must match the book the deposit joins ----
            # collateral and get_debt() are still the pre-deposit book. An empty
            # book has no ratio to preserve and no holders to trade against.
            if collateral != 0:
                # Both stablecoin ledgers, because an add with a nonzero pool
                # admin_fee moves a slice of the balances into admin_balances.
                added_debt: uint256 = xp[0] + admin - min(xp[0] + admin, old_stable + old_admin)
                proportional: uint256 = added_lp * staticcall LevAMM(AMM).get_debt() // collateral
                deviation: uint256 = (
                    added_debt - proportional if added_debt > proportional else proportional - added_debt
                )
                assert deviation * PRECISION <= proportional * RATIO_TOLERANCE, "LT deviation"

    # What is left is LP gifted to the LevAMM. Remember it for the next add.
    if surplus != old_surplus:
        self.amm_surplus = surplus
