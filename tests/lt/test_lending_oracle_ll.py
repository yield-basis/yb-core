"""
YBLendingOracleLL: EMA-smoothed ybBTC/BTC oracle.

The raw price equals YBLendingOracle.price_in_asset; LL applies a time-aware EMA
(alpha = exp(-dt/EMA_TIME)) on top, seeded lazily by the first price_w(). Checks:
  - unseeded / settled LL tracks the raw oracle (no distortion),
  - a step up in the raw price is lagged at dt~0 and converges over ~EMA_TIME (smoothing).
"""
import boa

EMA_TIME = 866   # half-life = EMA_TIME * ln(2) ~= 600s (10 min); must match the contract


def _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin):
    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)
    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(10**18, p * 10**18 // 10**18, 0)


def test_ll_ema(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = ll_deployer.deploy(yb_lt.address)
    yb = lending_oracle

    raw0 = yb.price_in_asset(yb_lt.address)
    # Unseeded: EMA returns the raw price (cached_price == 0).
    assert ll.price() == raw0
    # Seed; settled EMA of a constant == the constant.
    assert ll.price_w() == raw0
    assert ll.cached_price() == raw0
    assert ll.price() == raw0

    # Bump the raw price with wash round-trips (fees accrue into the pool -> LP value up).
    washer = accounts[3]
    collateral_token._mint_for_testing(washer, 1000 * 10**18)
    stablecoin._mint_for_testing(washer, 1000 * 100_000 * 10**18)
    with boa.env.prank(washer):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        for _ in range(200):
            got = cryptopool.exchange(0, 1, 5000 * 10**18, 0)
            cryptopool.exchange(1, 0, got, 0)

    raw1 = yb.price_in_asset(yb_lt.address)
    assert raw1 > raw0, "wash did not move the raw oracle"

    # dt ~ 0 since last price_w(): EMA still at the old cached value, NOT the bumped raw.
    p_immediate = ll.price()
    # one half-life (600s = 10 min) later: ~50% of the way to raw1.
    boa.env.time_travel(600)
    p_half = ll.price()
    # one full time-constant total: ~63% (1 - exp(-1)).
    boa.env.time_travel(EMA_TIME - 600)
    p_1tau = ll.price()
    # many time-constants later: converged to raw1.
    boa.env.time_travel(EMA_TIME * 8)
    p_settled = ll.price()

    move = raw1 - raw0
    print(f"\nraw0={raw0}  raw1={raw1}  move={move} ({move/raw0*100:.4f}%)")
    print(f"fraction moved: immediate={(p_immediate-raw0)/move:.3f}  "
          f"half(10min)={(p_half-raw0)/move:.3f}  1tau={(p_1tau-raw0)/move:.3f}  "
          f"settled={(p_settled-raw0)/move:.3f}")

    # Smoothing: the spike is essentially not reflected at dt~0...
    assert (p_immediate - raw0) <= move // 100               # < 1% of the move
    # ...~50% absorbed after one 10-minute half-life...
    assert 0.45 * move < (p_half - raw0) < 0.55 * move
    # ...~63% after one time constant (exp(-1) = 0.368 -> 0.632 moved)...
    assert 0.55 * move < (p_1tau - raw0) < 0.70 * move
    # ...and fully converged after several.
    assert abs(p_settled - raw1) < max(1, raw1 // 1000)      # within ~0.1%


def test_ll_ema_downward(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin,
    accounts, admin, yb_allocated, seed_cryptopool, ll_deployer, lending_oracle,
):
    _setup_position(cryptopool, yb_lt, collateral_token, stablecoin, accounts, admin)
    ll = ll_deployer.deploy(yb_lt.address)
    yb = lending_oracle

    raw0 = yb.price_in_asset(yb_lt.address)
    ll.price_w()   # seed at raw0

    # Crash the cryptopool collateral price (sell collateral); the EMA price_oracle then
    # drifts down over ~ma_time, dragging the raw ybBTC/BTC price down with it.
    dumper = accounts[3]
    collateral_token._mint_for_testing(dumper, 100 * 10**18)
    with boa.env.prank(dumper):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.exchange(1, 0, 12 * 10**18, 0)
    boa.env.time_travel(EMA_TIME)

    raw_mid = yb.price_in_asset(yb_lt.address)
    p_mid = ll.price()
    print(f"\nraw0={raw0}  raw_mid={raw_mid}  ll.price()={p_mid}")
    assert raw_mid < raw0, "price did not drop"
    # Downward smoothing (mirror of the upward case): the EMA lags ABOVE the dropped raw,
    # but has already started moving down from the seed.
    assert raw_mid < p_mid < raw0

    # Let both settle -> EMA converges down to the (lower) raw.
    boa.env.time_travel(EMA_TIME * 9)
    raw_settled = yb.price_in_asset(yb_lt.address)
    p_settled = ll.price()
    print(f"raw_settled={raw_settled}  ll.price()={p_settled}")
    assert abs(p_settled - raw_settled) < max(1, raw_settled // 1000)   # within ~0.1%
