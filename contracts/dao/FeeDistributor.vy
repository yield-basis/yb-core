# @version 0.4.3
"""
@title FeeDistributor
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Multi-token fee distribution for Yield Basis
"""
from ethereum.ercs import IERC20


WEEK: constant(uint256) = 7 * 86400
OVER_WEEKS: public(constant(uint256)) = 4
MAX_TOKENS: public(constant(uint256)) = 100
INITIAL_EPOCH: public(immutable(uint256))


last_claimed_for: public(HashMap[address, uint256])
token_sets: public(DynArray[IERC20,MAX_TOKENS][10**9])  # n_set -> token_set
current_token_set: public(uint256)
initial_set_for_epoch: public(HashMap[uint256, uint256])  # epoch_time -> token_set_id
balances_for_epoch: public(HashMap[uint256, HashMap[IERC20, uint256]])
token_balances: public(HashMap[IERC20, uint256])


@deploy
def __init__(token_set: DynArray[IERC20, MAX_TOKENS]):
    INITIAL_EPOCH = (block.timestamp + WEEK) // WEEK * WEEK
    self.token_sets[1] = token_set
    self.current_token_set = 1


@internal
def _fill_epochs():
    set_id: uint256 = self.current_token_set
    token_set: DynArray[IERC20, MAX_TOKENS] = self.token_sets[set_id]
    cursor: uint256 = (block.timestamp + WEEK) // WEEK * WEEK
    epochs: uint256[4] = [cursor, cursor + WEEK, cursor + 2*WEEK, cursor + 3*WEEK]

    for epoch: uint256 in epochs:
        if self.initial_set_for_epoch[epoch] == 0:
            self.initial_set_for_epoch[epoch] = set_id

    for token: IERC20 in token_set:
        balance: uint256 = staticcall token.balanceOf(self)
        old_balance: uint256 = self.token_balances[token]
        if balance > old_balance:
            self.token_balances[token] = balance
            balance_per_epoch: uint256 = (balance - old_balance) // OVER_WEEKS
            for epoch: uint256 in epochs:
                self.balances_for_epoch[epoch][token] += balance_per_epoch


@external
def fill_epochs():
    self._fill_epochs()
# When claiming all - check ALL tokens in 4 epochs
