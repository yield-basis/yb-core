#!/usr/bin/env python3

import boa
import os
import json

from eth_account import account
from getpass import getpass
from networks import NETWORK


FORK = False
VOTING_PLUGIN = "0xD4f8EaCE89891e89FA46eE60B02a48D3d0FD137C"
USER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK)
        boa.env.eoa = USER
    else:
        boa.set_network_env(NETWORK)
        USER = account_load('yb-deployer')
        boa.env.add_account(USER)

    voting = boa.load_abi(os.path.dirname(__file__) + '/TokenVoting.abi.json', name="AragonVoting").at(VOTING_PLUGIN)

    proposal_id = 9853133070794896715491688810991087743550510388425306464155210411897148889322
    # Yes = 2, No = 3
    proposal_id = voting.vote(proposal_id, 2, True)
