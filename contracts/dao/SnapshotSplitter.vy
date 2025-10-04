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


struct WeightedVote:
    vid: uint256
    weight: uint256
    block: uint256


ARAGON: immutable(Aragon)
VE: immutable(VeCRV)
splits: HashMap[uint256, HashMap[address, uint256]]
weighted_votes: WeightedVote[10]


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
    assert yay > 0 and nay > 0
    self.splits[vote_id][voter] = 10**18 * yay // (yay + nay)


@external
def register_votes(vote_ids: DynArray[uint256, 10], weights: DynArray[uint256, 10]):
    total_weight: uint256 = 0
    for w: uint256 in weights:
        total_weight += w
    i: uint256 = 0
    for vid: uint256 in vote_ids:
        state: VoteState = staticcall ARAGON.getVote(vid)
        self.weighted_votes[i] = WeightedVote(
            vid=vid,
            weight=(total_weight * state.yea // weights[i] + 1),  # We will divide ve amount by this, so it has to be LARGER than the original weight
            block=state.snapshotBlock
        )
        i += 1


@external
@view
def get_fraction(voter: address) -> uint256:
    weight: uint256 = 0

    for i: uint256 in range(10):
        wv: WeightedVote = self.weighted_votes[i]
        if wv.block == 0:
            break

        vote: uint8 = staticcall ARAGON.getVoterState(wv.vid, voter)
        if vote > 0:
            split: uint256 = self.splits[wv.vid][voter]
            if split == 0:
                if vote == 1:
                    split = 10**18
                elif vote == 2:
                    split = 0
                elif vote == 3:
                    split = 5 * 10**17
            weight += (staticcall VE.balanceOfAt(voter, wv.block)) * split // wv.weight

    return weight
