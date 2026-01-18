import boa


def test_forked_network(forked_env):
    """Dummy test to verify the forked network fixture works."""
    # Check that we can query the chain
    block_number = boa.env.evm.patch.block_number
    assert block_number > 0, "Should be connected to a forked network with blocks"
