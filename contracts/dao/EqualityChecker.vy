# @version 0.4.3
"""
@title EqualityChecker
@author Yield Basis
@license MIT
@notice Calls a view method by selector and asserts the result equals (or doesn't equal) an expected value
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
