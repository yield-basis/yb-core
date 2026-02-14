# @version 0.4.3
"""
@title CallComparator
@author Yield Basis
@license MIT
@notice Calls a view method by selector and asserts the result satisfies a comparison against an expected value
"""


@external
def check_equal(target: address, selector: Bytes[4], expected: uint256):
    """
    @notice Call a view method and revert if the result != expected
    @param target Contract to call
    @param selector 4-byte function selector of a view method returning uint256
    @param expected Value the result must equal
    """
    response: Bytes[32] = raw_call(target, selector, max_outsize=32, is_static_call=True)
    assert convert(response, uint256) == expected, "Not equal"


@external
def check_nonequal(target: address, selector: Bytes[4], expected: uint256):
    """
    @notice Call a view method and revert if the result == expected
    @param target Contract to call
    @param selector 4-byte function selector of a view method returning uint256
    @param expected Value the result must not equal
    """
    response: Bytes[32] = raw_call(target, selector, max_outsize=32, is_static_call=True)
    assert convert(response, uint256) != expected, "Equal"


@external
def check_gt(target: address, selector: Bytes[4], expected: uint256):
    """
    @notice Call a view method and revert if the result <= expected
    @param target Contract to call
    @param selector 4-byte function selector of a view method returning uint256
    @param expected Value the result must be greater than
    """
    response: Bytes[32] = raw_call(target, selector, max_outsize=32, is_static_call=True)
    assert convert(response, uint256) > expected, "Not greater"


@external
def check_lt(target: address, selector: Bytes[4], expected: uint256):
    """
    @notice Call a view method and revert if the result >= expected
    @param target Contract to call
    @param selector 4-byte function selector of a view method returning uint256
    @param expected Value the result must be less than
    """
    response: Bytes[32] = raw_call(target, selector, max_outsize=32, is_static_call=True)
    assert convert(response, uint256) < expected, "Not less"


@external
def check_timestamp_gt(expected: uint256):
    """
    @notice Revert if block.timestamp <= expected
    @param expected Value that block.timestamp must be greater than
    """
    assert block.timestamp > expected, "Timestamp not greater"


@external
def check_timestamp_lt(expected: uint256):
    """
    @notice Revert if block.timestamp >= expected
    @param expected Value that block.timestamp must be less than
    """
    assert block.timestamp < expected, "Timestamp not less"


last_called: public(HashMap[address, uint256])

@external
def check_called_after(min_delay: uint256):
    """
    @notice Record the current timestamp for msg.sender and revert if
            the sender has not called this method at least min_delay seconds ago
    @param min_delay Minimum number of seconds since the sender's previous call
    """
    assert block.timestamp >= self.last_called[msg.sender] + min_delay, "Too early"
    self.last_called[msg.sender] = block.timestamp
