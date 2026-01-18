# @version 0.4.3
"""
@notice Minimal WETH interface for testing
"""

event Transfer:
    _from: indexed(address)
    _to: indexed(address)
    _value: uint256

event Approval:
    _owner: indexed(address)
    _spender: indexed(address)
    _value: uint256

name: public(String[64])
symbol: public(String[32])
decimals: public(uint256)
balanceOf: public(HashMap[address, uint256])
allowance: public(HashMap[address, HashMap[address, uint256]])


@external
@payable
def deposit():
    self.balanceOf[msg.sender] += msg.value
    log Transfer(_from=empty(address), _to=msg.sender, _value=msg.value)


@external
def withdraw(amount: uint256):
    self.balanceOf[msg.sender] -= amount
    send(msg.sender, amount)
    log Transfer(_from=msg.sender, _to=empty(address), _value=amount)


@external
def transfer(_to: address, _value: uint256) -> bool:
    self.balanceOf[msg.sender] -= _value
    self.balanceOf[_to] += _value
    log Transfer(_from=msg.sender, _to=_to, _value=_value)
    return True


@external
def transferFrom(_from: address, _to: address, _value: uint256) -> bool:
    self.balanceOf[_from] -= _value
    self.balanceOf[_to] += _value
    self.allowance[_from][msg.sender] -= _value
    log Transfer(_from=_from, _to=_to, _value=_value)
    return True


@external
def approve(_spender: address, _value: uint256) -> bool:
    self.allowance[msg.sender][_spender] = _value
    log Approval(_owner=msg.sender, _spender=_spender, _value=_value)
    return True


@external
@view
def totalSupply() -> uint256:
    return self.balance
