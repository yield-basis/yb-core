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
