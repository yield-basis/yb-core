from snekmate.auth import ownable
from snekmate.tokens import erc20


initializes: ownable
initializes: erc20[ownable := ownable]


exports: (
    erc20.IERC20,
    erc20.IERC20Detailed,
    ownable.renounce_ownership
)


@deploy
def __init__():
    ownable.__init__()
    erc20.__init__("Yield Basis", "YB", 18, "Just say no", "to EIP712")
    # Ownership is now with msg.sender
    # Sender should revoke it once the setup is complete
