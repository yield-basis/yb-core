# @version 0.4.3
"""
@title YBLendingOracleLLFactory
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Deploys per market a YBLendingOracleLL clone (asset-denominated, owns the EMA) plus a
        YBLendingOracleLLUSD forwarder to it (USD-denominated, same EMA), and lets the DAO (set
        at deploy, transferable via set_dao) retune a market's EMA time. create_oracles is
        permissionless; the LT is resolved from the YB Factory market id.
"""

interface Factory:
    def markets(i: uint256) -> Market: view
    def market_count() -> uint256: view

interface LendingOracleLL:
    def initialize(lt: address, ema_time: uint256, factory: address): nonpayable
    def set_ema_time(ema_time: uint256): nonpayable


struct Market:
    asset_token: address
    cryptopool: address
    amm: address
    lt: address
    price_oracle: address
    virtual_pool: address
    staker: address


event OraclesCreated:
    market_id: indexed(uint256)
    lt: indexed(address)
    usd_oracle: address
    asset_oracle: address
    ema_time: uint256

event SetDefaultEmaTime:
    ema_time: uint256

event SetEmaTime:
    market_id: indexed(uint256)
    oracle: indexed(address)
    ema_time: uint256

event SetDao:
    dao: indexed(address)


# Sane upper bound on the EMA time constant (~31.7 yr); mirrors YBLendingOracleLL.MAX_EMA_TIME.
MAX_EMA_TIME: constant(uint256) = 10**9

FACTORY: public(immutable(Factory))       # YB Factory: market-id -> LT
LL_IMPL: public(immutable(address))       # YBLendingOracleLL implementation cloned per market
USD_BLUEPRINT: public(immutable(address)) # YBLendingOracleLLUSD blueprint deployed per market

dao: public(address)                      # may retune ema_time; the YB Factory admin (DAO)
default_ema_time: public(uint256)         # EMA time (s) stamped on newly created clones

# market_id -> spawned oracle (0 until created). asset_oracle is the YBLendingOracleLL clone
# holding the EMA; usd_oracle is the YBLendingOracleLLUSD forwarder to it.
usd_oracle: public(HashMap[uint256, address])
asset_oracle: public(HashMap[uint256, address])


@deploy
def __init__(factory: Factory, ll_impl: address, usd_blueprint: address, default_ema_time: uint256, dao: address):
    """
    @notice Bind the factory to the YB factory, the oracle implementations and the DAO.
    @param factory YB factory, used to resolve market-id -> LT
    @param ll_impl YBLendingOracleLL implementation cloned per market
    @param usd_blueprint YBLendingOracleLLUSD blueprint (EIP-5202) deployed per market
    @param default_ema_time EMA time constant (s) stamped on new clones (0 < t <= MAX_EMA_TIME)
    @param dao Address allowed to retune ema_time (the YB Factory admin); transferable via set_dao
    """
    assert factory.address != empty(address) and ll_impl != empty(address), "Zero"
    assert usd_blueprint != empty(address) and dao != empty(address), "Zero"
    assert default_ema_time > 0 and default_ema_time <= MAX_EMA_TIME, "ema_time"
    FACTORY = factory
    LL_IMPL = ll_impl
    USD_BLUEPRINT = usd_blueprint
    self.dao = dao
    self.default_ema_time = default_ema_time


@external
def create_oracles(market_id: uint256) -> (address, address):
    """
    @notice Spawn the USD + asset EMA oracles for a YB Factory market. Callable by anyone; the
            LT is resolved (and existence checked) via the market id. Idempotent: returns the
            existing pair if already created.
    @param market_id Index of the market in the YB factory
    @return (usd_oracle, asset_oracle) addresses
    """
    usd: address = self.usd_oracle[market_id]
    if usd != empty(address):
        return (usd, self.asset_oracle[market_id])

    assert market_id < staticcall FACTORY.market_count(), "No market"
    lt: address = (staticcall FACTORY.markets(market_id)).lt
    assert lt != empty(address), "No market"

    ema_time: uint256 = self.default_ema_time
    asset: address = create_minimal_proxy_to(LL_IMPL)
    extcall LendingOracleLL(asset).initialize(lt, ema_time, self)
    usd = create_from_blueprint(USD_BLUEPRINT, asset)

    self.usd_oracle[market_id] = usd
    self.asset_oracle[market_id] = asset
    log OraclesCreated(market_id=market_id, lt=lt, usd_oracle=usd, asset_oracle=asset, ema_time=ema_time)
    return (usd, asset)


@external
def set_ema_time(market_id: uint256, ema_time: uint256):
    """
    @notice Retune the EMA time of a market's oracle - shared by its USD and asset prices.
            YB Factory admin (DAO) only.
    @param market_id Index of the market whose oracle to retune
    @param ema_time New EMA time constant in seconds (0 < t <= MAX_EMA_TIME)
    """
    assert msg.sender == self.dao, "Not DAO"
    oracle: address = self.asset_oracle[market_id]
    assert oracle != empty(address), "No oracles"
    extcall LendingOracleLL(oracle).set_ema_time(ema_time)
    log SetEmaTime(market_id=market_id, oracle=oracle, ema_time=ema_time)


@external
def set_default_ema_time(ema_time: uint256):
    """
    @notice Set the EMA time stamped on future clones. DAO only. Does not touch existing clones.
    @param ema_time New default EMA time constant in seconds (0 < t <= MAX_EMA_TIME)
    """
    assert msg.sender == self.dao, "Not DAO"
    assert ema_time > 0 and ema_time <= MAX_EMA_TIME, "ema_time"
    self.default_ema_time = ema_time
    log SetDefaultEmaTime(ema_time=ema_time)


@external
def set_dao(dao: address):
    """
    @notice Transfer the EMA-time admin to a new DAO. Current DAO only.
    @param dao The new DAO address (nonzero)
    """
    assert msg.sender == self.dao, "Not DAO"
    assert dao != empty(address), "Zero"
    self.dao = dao
    log SetDao(dao=dao)
