#!/usr/bin/env python3

import boa
from networks import NETWORK
from networks import ETHERSCAN_API_KEY


FORK = True
ARAGON = "0xE478de485ad2fe566d49342Cbd03E49ed7DB3356"
VE = "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2"
DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer

VOTE_IDS = [1206, 1213]
SPLITS = {1206: (398069876760505032592651518, 19719666439017461732129672)}
VOTE_SPLITTING_USER = "0x989AEb4d175e16225E39E87d0D97A3360524AD80"  # Works if there's only one (our case - that's convex)


def account_load(fname):
    path = os.path.expanduser(os.path.join('~', '.brownie', 'accounts', fname + '.json'))
    with open(path, 'r') as f:
        pkey = account.decode_keyfile_json(json.load(f), getpass())
        return account.Account.from_key(pkey)


if __name__ == '__main__':
    if FORK:
        boa.fork(NETWORK)
    else:
        boa.set_network_env(NETWORK)
        etherscan = Etherscan(api_key=ETHERSCAN_API_KEY)

    if FORK:
        admin = DEPLOYER
        boa.env.eoa = admin
    else:
        admin = account_load('yb-deployer')
        boa.env.add_account(admin)

    splitter = boa.load('contracts/dao/SnapshotSplitter.vy', ARAGON, VE)
    for vote_id, (yay, nay) in SPLITS.items():
        splitter.register_split(vote_id, VOTE_SPLITTING_USER, yay, nay)

    for vote_id in VOTE_IDS:
        yes, no, total = splitter.get_vote(vote_id)
        print(f'Yes: {yes/1e18}, No: {no/1e18}, Total: {total/1e18}')
        vote = splitter.get_aragon_vote(vote_id, VOTE_SPLITTING_USER)
        print(f'Test vote: {vote/1e18}')
