# @version 0.4.3
"""
@title FeeDistributor
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Multi-token fee distribution for Yield Basis
"""
from snekmate.auth import ownable
from ethereum.ercs import IERC20


initializes: ownable

exports: (
    ownable.transfer_ownership,
    ownable.owner
)


interface VotingEscrow:
    def getPastVotes(account: address, timepoint: uint256) -> uint256: view
    def getPastTotalSupply(timepoint: uint256) -> uint256: view


event FundEpoch:
    epoch: indexed(uint256)
    token: indexed(IERC20)
    amount: uint256

event AddTokenSet:
    token_set_id: indexed(uint256)
    token_set: DynArray[IERC20,MAX_TOKENS]

event Claim:
    user: indexed(address)
    token: indexed(IERC20)
    amount: uint256


WEEK: constant(uint256) = 7 * 86400
OVER_WEEKS: public(constant(uint256)) = 4
MAX_TOKENS: public(constant(uint256)) = 100
INITIAL_EPOCH: public(immutable(uint256))
VE: public(immutable(VotingEscrow))


last_claimed_for: public(HashMap[address, uint256])
token_sets: public(DynArray[IERC20,MAX_TOKENS][10**9])  # n_set -> token_set
current_token_set: public(uint256)
initial_set_for_epoch: public(HashMap[uint256, uint256])  # epoch_time -> token_set_id
max_set_for_epoch: public(HashMap[uint256, uint256])
balances_for_epoch: public(HashMap[uint256, HashMap[IERC20, uint256]])
token_balances: public(HashMap[IERC20, uint256])
claimed_for: public(HashMap[uint256, HashMap[address, HashMap[IERC20, bool]]])

user_claim_id: public(uint256)
user_claimed_tokens: public(HashMap[address, HashMap[uint256, HashMap[IERC20, uint256]]])


@deploy
def __init__(token_set: DynArray[IERC20, MAX_TOKENS], ve: VotingEscrow, owner: address):
    INITIAL_EPOCH = (block.timestamp + WEEK) // WEEK * WEEK
    VE = ve
    self.token_sets[1] = token_set
    self.current_token_set = 1
    log AddTokenSet(token_set_id=1, token_set=token_set)

    ownable.__init__()
    ownable._transfer_ownership(owner)


@internal
def _fill_epochs():
    set_id: uint256 = self.current_token_set
    token_set: DynArray[IERC20, MAX_TOKENS] = self.token_sets[set_id]
    cursor: uint256 = (block.timestamp + WEEK) // WEEK * WEEK
    epochs: uint256[4] = [cursor, cursor + WEEK, cursor + 2*WEEK, cursor + 3*WEEK]

    for epoch: uint256 in epochs:
        if self.initial_set_for_epoch[epoch] == 0:
            self.initial_set_for_epoch[epoch] = set_id
        self.max_set_for_epoch[epoch] = set_id

    for token: IERC20 in token_set:
        balance: uint256 = staticcall token.balanceOf(self)
        old_balance: uint256 = self.token_balances[token]
        if balance > old_balance:
            self.token_balances[token] = balance
            balance_per_epoch: uint256 = (balance - old_balance) // OVER_WEEKS
            for epoch: uint256 in epochs:
                self.balances_for_epoch[epoch][token] += balance_per_epoch
                log FundEpoch(epoch=epoch, token=token, amount=balance_per_epoch)


@external
def fill_epochs():
    self._fill_epochs()


@external
def add_token_set(token_set: DynArray[IERC20, MAX_TOKENS]):
    ownable._check_owner()
    self._fill_epochs()
    current_set_id: uint256 = self.current_token_set
    current_set_id += 1
    self.current_token_set = current_set_id
    self.token_sets[current_set_id] = token_set
    log AddTokenSet(token_set_id=current_set_id, token_set=token_set)


@external
def claim(user: address = msg.sender, epoch_count: uint256 = 50):
    self._fill_epochs()

    epoch: uint256 = self.last_claimed_for[user]
    if epoch == 0:
        epoch = INITIAL_EPOCH
    else:
        epoch += WEEK
    save_epoch: uint256 = 0
    user_claim_id: uint256 = 0
    if epoch <= block.timestamp:
        user_claim_id = self.user_claim_id
        self.user_claim_id = user_claim_id + 1
    tokens_to_claim: DynArray[IERC20, MAX_TOKENS * 4] = empty(DynArray[IERC20, MAX_TOKENS * 4])

    for i: uint256 in range(50):
        if epoch > block.timestamp or i >= epoch_count:
            break

        else:
            save_epoch = epoch
            votes: uint256 = staticcall VE.getPastVotes(user, epoch)
            total_votes: uint256 = staticcall VE.getPastTotalSupply(epoch)

            ts_id: uint256 = self.initial_set_for_epoch[epoch]
            max_ts_id: uint256 = self.max_set_for_epoch[epoch]
            for j: uint256 in range(50):
                for token: IERC20 in self.token_sets[ts_id]:
                    if not self.claimed_for[epoch][user][token]:
                        amount: uint256 = self.balances_for_epoch[epoch][token] * votes // total_votes
                        if amount > 0:
                            old_amount: uint256 = self.user_claimed_tokens[user][user_claim_id][token]
                            if old_amount == 0:
                                tokens_to_claim.append(token)
                            self.user_claimed_tokens[user][user_claim_id][token] = old_amount + amount
                        self.claimed_for[epoch][user][token] = True
                ts_id += 1
                if ts_id > max_ts_id:
                    break

    if save_epoch > 0:
        self.last_claimed_for[user] = epoch
        for token: IERC20 in tokens_to_claim:
            amount: uint256 = self.user_claimed_tokens[user][user_claim_id][token]
            assert extcall token.transfer(user, amount, default_return_value=True)
            log Claim(user=user, token=token, amount=amount)


@external
def recover_token(token: IERC20, receiver: address):
    ownable._check_owner()
    amount: uint256 = (staticcall token.balanceOf(self)) - self.token_balances[token]
    if amount > 0:
        assert extcall token.transfer(receiver, amount, default_return_value=True)
