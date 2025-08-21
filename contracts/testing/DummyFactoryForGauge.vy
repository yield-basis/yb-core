# @version 0.4.3

"""
This contract is for testing only.
If you see it on mainnet - it won't be used for anything except testing the actual deployment
"""

gauge_controller: public(immutable(address))
emergency_admin: public(immutable(address))
admin: public(immutable(address))


@deploy
def __init__(_admin: address, _gauge_controller: address):
    admin = _admin
    emergency_admin = _admin
    gauge_controller = _gauge_controller
