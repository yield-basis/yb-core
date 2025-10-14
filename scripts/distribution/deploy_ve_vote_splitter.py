#!/usr/bin/env python3

import boa
from networks import NETWORK
from networks import ETHERSCAN_API_KEY


FORK = True
ARAGON = "0xE478de485ad2fe566d49342Cbd03E49ed7DB3356"
VE = "0x5f3b5DfEb7B28CDbD7FAba78963EE202a494e2A2"
YB = "0x01791F726B4103694969820be083196cC7c045fF"

DEPLOYER = "0xa39E4d6bb25A8E55552D6D9ab1f5f8889DDdC80d"  # YB Deployer
TEST_YB_HOLDER = "0x42F2A41A0D0e65A440813190880c8a65124895Fa"

VOTE_IDS = [1206, 1213]
SPLITS = {1206: (398069876760505032592651518, 19719666439017461732129672)}
VOTE_SPLITTING_USER = "0x989AEb4d175e16225E39E87d0D97A3360524AD80"  # Works if there's only one (our case - that's convex)
USER_MAPPINGS = {"0x989AEb4d175e16225E39E87d0D97A3360524AD80": "0x1389388d01708118b497f59521f6943Be2541bb7"}


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

    splitter = boa.load('contracts/dao/SnapshotSplitter.vy', ARAGON, VE, YB)
    for vote_id, (yay, nay) in SPLITS.items():
        splitter.register_split(vote_id, VOTE_SPLITTING_USER, yay, nay)
    splitter.register_votes(VOTE_IDS, [2, 1])
    splitter.register_mappings(
        list(USER_MAPPINGS.keys()),
        list(USER_MAPPINGS.values())
    )

    yb_interface = boa.load_partial('contracts/dao/YB.vy')
    yb = yb_interface.at(YB)

    if FORK:
        with boa.env.prank(TEST_YB_HOLDER):
            yb.transfer(splitter.address, 5 * 10**6 * 10**18)

    splitter.renounce_ownership()

    if FORK:
        TEST_USERS = [
            "0x989AEb4d175e16225E39E87d0D97A3360524AD80",
            "0x52f541764E6e90eeBc5c21Ff570De0e2D63766B6",
            "0xF147b8125d2ef93FB6965Db97D6746952a133934",
            "0x9B44473E223f8a3c047AD86f387B80402536B029",
            "0x7a16fF8270133F063aAb6C9977183D9e72835428",
            "0xF89501B77b2FA6329F94F5A05FE84cEbb5c8b1a0",
            "0x425d16B0e08a28A3Ff9e4404AE99D78C0a076C5A",
            "0x32D03DB62e464c9168e41028FFa6E9a05D8C6451",
            "0x0D5Dc686d0a2ABBfDaFDFb4D0533E886517d4E83",
            "0x10E3085127C9BD92AB325F8D1f65CDcEC2436149",
            "0x39415255619783A2E71fcF7d8f708A951d92e1b6"
        ]
        fracs = [splitter.get_fraction(u) for u in TEST_USERS]
        print(f'Fraction: {sum(fracs)/1e18}')
        claimed = {}
        for user in TEST_USERS:
            with boa.env.prank(user):
                splitter.claim()
                with boa.reverts():
                    splitter.claim()
                claimed[user] = yb.balanceOf(USER_MAPPINGS.get(user, user))
        print(f"Total claimed: {sum(claimed.values()) / 1e18}")
        from pprint import pprint
        pprint(claimed)
