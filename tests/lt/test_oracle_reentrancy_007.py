"""
ChainSecurity #007 — read-only reentrancy guard in YBLendingOracle.

The oracle now probes amm.check_nonreentrant() (an @nonreentrant @view that reverts when
the AMM's lock is held) before reading AMM state. There is no real reentrancy path in the
current system (plain ERC-20s, no hooks), so we verify the guard mechanism with a mock AMM
whose check_nonreentrant() reverts -- standing in for "lock held" -- and confirm the oracle
refuses to price (reverts) rather than reading mid-operation state.
"""
import boa


LOCKED_AMM = """
# pragma version 0.4.3
@external
@view
def check_nonreentrant():
    raw_revert(b"locked")  # simulate: reentrancy lock is held
"""

MOCK_AGG = """
# pragma version 0.4.3
@external
@view
def price() -> uint256:
    return 10**18
"""

# Minimal LT: the oracle reads CRYPTOPOOL()/amm()/agg() before the guard fires.
MOCK_LT = """
# pragma version 0.4.3
pool: address
amm_a: address
agg_a: address

@deploy
def __init__(p: address, a: address, g: address):
    self.pool = p
    self.amm_a = a
    self.agg_a = g

@external
@view
def CRYPTOPOOL() -> address:
    return self.pool

@external
@view
def amm() -> address:
    return self.amm_a

@external
@view
def agg() -> address:
    return self.agg_a
"""


def test_oracle_reverts_when_amm_lock_held(lending_oracle):
    oracle = lending_oracle
    locked_amm = boa.loads(LOCKED_AMM)
    agg = boa.loads(MOCK_AGG)
    mock_lt = boa.loads(MOCK_LT, locked_amm.address, locked_amm.address, agg.address)

    # check_nonreentrant() reverts ("lock held") -> the oracle must refuse to price.
    with boa.reverts("AMM reentrancy"):
        oracle.price_in_usd(mock_lt.address)
    with boa.reverts("AMM reentrancy"):
        oracle.price_in_asset(mock_lt.address)
