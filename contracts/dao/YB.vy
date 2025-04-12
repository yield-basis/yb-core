from snekmate.auth import ownable
from snekmate.tokens import erc20


initializes: ownable
initializes: erc20[ownable := ownable]


exports: (
    erc20.IERC20,
    erc20.IERC20Detailed
)


@deploy
def __init__():
    ownable.__init__()
    erc20.__init__("Yield Basis", "YB", 18, "Just say no", "to EIP712")
    ownable._transfer_ownership(empty(address))
