# @version 0.4.3
"""
@title YBLendingOracleLLFactory
@author Yield Basis
@license GNU Affero General Public License v3.0
@notice Deploys per-market YBLendingOracleLL clones (a USD one and an asset one) and lets the
        DAO (set at deploy, transferable via set_dao) retune any clone's EMA time. create_oracles
        is permissionless; the LT is resolved from the YB Factory market id.
"""

interface Factory:
    def markets(i: uint256) -> Market: view
    def market_count() -> uint256: view

interface LendingOracleLL:
    def initialize(lt: address, in_usd: bool, ema_time: uint256, factory: address): nonpayable
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
    oracle: indexed(address)
    ema_time: uint256

event SetDao:
    dao: indexed(address)


# Sane upper bound on the EMA time constant (~31.7 yr); mirrors YBLendingOracleLL.MAX_EMA_TIME.
MAX_EMA_TIME: constant(uint256) = 10**9

FACTORY: public(immutable(Factory))       # YB Factory: market-id -> LT
LL_IMPL: public(immutable(address))       # YBLendingOracleLL implementation cloned per market

dao: public(address)                      # may retune ema_time; the YB Factory admin (DAO)
default_ema_time: public(uint256)         # EMA time (s) stamped on newly created clones

# market_id -> spawned EMA oracle clone (0 until created)
usd_oracle: public(HashMap[uint256, address])
asset_oracle: public(HashMap[uint256, address])


@deploy
def __init__(factory: Factory, ll_impl: address, default_ema_time: uint256, dao: address):
    """
    @notice Bind the factory to the YB factory, the YBLendingOracleLL implementation and the DAO.
    @param factory YB factory, used to resolve market-id -> LT
    @param ll_impl YBLendingOracleLL implementation cloned per (market, denomination)
    @param default_ema_time EMA time constant (s) stamped on new clones (0 < t <= MAX_EMA_TIME)
    @param dao Address allowed to retune ema_time (the YB Factory admin); transferable via set_dao
    """
    assert factory.address != empty(address) and ll_impl != empty(address), "Zero"
    assert dao != empty(address), "Zero"
    assert default_ema_time > 0 and default_ema_time <= MAX_EMA_TIME, "ema_time"
    FACTORY = factory
    LL_IMPL = ll_impl
    self.dao = dao
    self.default_ema_time = default_ema_time


@external
def create_oracles(market_id: uint256) -> (address, address):
    """
    @notice Spawn the USD + asset EMA oracle clones for a YB Factory market. Callable by
            anyone; the LT is resolved (and existence checked) via the market id. Idempotent:
            returns the existing pair if already created.
    @param market_id Index of the market in the YB factory
    @return (usd_oracle, asset_oracle) clone addresses
    """
    usd: address = self.usd_oracle[market_id]
    if usd != empty(address):
        return (usd, self.asset_oracle[market_id])

    assert market_id < staticcall FACTORY.market_count(), "No market"
    lt: address = (staticcall FACTORY.markets(market_id)).lt
    assert lt != empty(address), "No market"

    ema_time: uint256 = self.default_ema_time
    usd = create_minimal_proxy_to(LL_IMPL)
    asset: address = create_minimal_proxy_to(LL_IMPL)
    extcall LendingOracleLL(usd).initialize(lt, True, ema_time, self)
    extcall LendingOracleLL(asset).initialize(lt, False, ema_time, self)

    self.usd_oracle[market_id] = usd
    self.asset_oracle[market_id] = asset
    log OraclesCreated(market_id=market_id, lt=lt, usd_oracle=usd, asset_oracle=asset, ema_time=ema_time)
    return (usd, asset)


@external
def set_ema_time(oracle: address, ema_time: uint256):
    """
    @notice Retune the EMA time of a single clone. YB Factory admin (DAO) only.
    @param oracle A YBLendingOracleLL clone created by this factory
    @param ema_time New EMA time constant in seconds (0 < t <= MAX_EMA_TIME)
    """
    assert msg.sender == self.dao, "Not DAO"
    extcall LendingOracleLL(oracle).set_ema_time(ema_time)
    log SetEmaTime(oracle=oracle, ema_time=ema_time)


@external
def set_ema_time_market(market_id: uint256, ema_time: uint256):
    """
    @notice Retune the EMA time of BOTH clones (USD + asset) of a market. DAO only.
    @param market_id Index of the market whose oracle pair to retune
    @param ema_time New EMA time constant in seconds (0 < t <= MAX_EMA_TIME)
    """
    assert msg.sender == self.dao, "Not DAO"
    usd: address = self.usd_oracle[market_id]
    assert usd != empty(address), "No oracles"
    extcall LendingOracleLL(usd).set_ema_time(ema_time)
    extcall LendingOracleLL(self.asset_oracle[market_id]).set_ema_time(ema_time)
    log SetEmaTime(oracle=usd, ema_time=ema_time)
    log SetEmaTime(oracle=self.asset_oracle[market_id], ema_time=ema_time)


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
