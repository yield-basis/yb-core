# @version 0.4.3
"""
@title Snapshot Splitter
@author Yield Basis
@license MIT
@notice Splits allocation of deposited tokens towards veCRV (old Aragon) balances who voted for specified votes
"""
from snekmate.auth import ownable
from ethereum.ercs import IERC20


initializes: ownable

exports: (
    ownable.renounce_ownership,
    ownable.owner
)


interface Aragon:
    # VoterState: 0 = Absent, 1 = Yes, 2 = No, 3 = Even
    def getVoterState(_voteID: uint256, _voter: address) -> uint8: view
    def getVote(_voteID: uint256) -> VoteState: view

interface VeCRV:
    def balanceOfAt(user: address, block: uint256) -> uint256: view


struct VoteState:
    open: bool
    executed: bool
    startDate: uint64
    snapshotBlock: uint256
    supportRequired: uint64
    minAcceptQuorum: uint64
    yea: uint256
    nay: uint256
    votingPower: uint256
    script: Bytes[1000]


ARAGON: immutable(Aragon)
VE: immutable(VeCRV)
splits: HashMap[uint256, HashMap[address, uint256]]


@deploy
def __init__(aragon: Aragon, ve: VeCRV):
    """
    For Curve voting: Aragon = 0xE478de485ad2fe566d49342Cbd03E49ed7DB3356
    """
    ownable.__init__()
    ARAGON = aragon
    VE = ve


@external
def register_split(vote_id: uint256, voter: address, yay: uint256, nay: uint256):
    ownable._check_owner()
    self.splits[vote_id][voter] = 10**18 * yay // (yay + nay)


@external
@view
def get_aragon_vote(vote_id: uint256, voter: address) -> uint256:
    vote: uint8 = staticcall ARAGON.getVoterState(vote_id, voter)

    if vote == 0:
        return 0

    else:
        state: VoteState = staticcall ARAGON.getVote(vote_id)
        power: uint256 = staticcall VE.balanceOfAt(voter, state.snapshotBlock)
        split: uint256 = self.splits[vote_id][voter]

        if split == 0:
            if vote == 1:
                return power
            elif vote == 3:
                return power // 2
            else:
                return 0

        else:
            return power * split // 10**18

@external
@view
def get_vote(vote_id: uint256) -> (uint256, uint256, uint256):
    state: VoteState = staticcall ARAGON.getVote(vote_id)
    return state.yea, state.nay, state.votingPower
