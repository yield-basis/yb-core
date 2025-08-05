# @version 0.4.3
"""
@title VotingPowerCondition
@author Yield Basis
@license MIT
@notice Smart contract based on this one to check limits for voting power:
    https://github.com/aragon/token-voting-plugin/blob/main/packages/contracts/src/VotingPowerCondition.sol
"""

VE: public(immutable(VotingEscrow))
MIN_POWER: public(immutable(uint256))


_SUPPORTED_INTERFACES: constant(bytes4[2]) = [
    0x01FFC9A7,  # The ERC-165 identifier for ERC-165.
    0x2675fdd0  # IPermissionCondition
]


interface VotingEscrow:
    def getVotes(account: address) -> uint256: view


@deploy
def __init__(ve: VotingEscrow, min_power: uint256):
    VE = ve
    MIN_POWER = min_power


@external
@view
def supportsInterface(interface_id: bytes4) -> bool:
    """
    @dev Returns `True` if this contract implements the
         interface defined by `interface_id`.
    @param interface_id The 4-byte interface identifier.
    @return bool The verification whether the contract
            implements the interface or not.
    """
    return interface_id in _SUPPORTED_INTERFACES


@external
@view
def isGranted(_where: address, _who: address, _permission_id: bytes32, calldata: Bytes[2000]) -> bool:
    if staticcall VE.getVotes(_who) >= MIN_POWER:
        return True
    else:
        return False
