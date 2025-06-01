# @version 0.4.1

"""
This contract is for testing only.
If you see it on mainnet - it won't be used for anything except testing the actual deployment
"""

GAUGE_CONTROLLER: public(immutable(address))
admin: public(immutable(address))


@deploy
def __init__(_admin: address, _gauge_controller: address):
    admin = _admin
    GAUGE_CONTROLLER = _gauge_controller

