"""
Fork tests for YBNetPressure against the live YB markets, real Curve pools and the
real crvUSD price aggregator, pinned to a fixed block (conftest.FORK_BLOCK) so the
numbers are reproducible run-to-run.

Two things the per-market-aggregator optimization relies on are pinned here on real
state (at FORK_BLOCK the aggregator reports 1.0000962, i.e. NOT exactly 1.0, so the
agg factor is genuinely exercised):

  1. All markets share one aggregator: Factory.agg() == LT.agg() for every market, and
     the AMM's PRICE_ORACLE_CONTRACT.price() is reconstructable as
     lp_price_ps * agg_price / 1e18. This is why PID can read the aggregator once
     (from the Factory) and apply it to every market.
  2. Passing that shared agg_price in is transparent: net_pressure_and_tvl(lt, agg_price)
     (and the two single-value entry points) are bit-for-bit identical to the
     agg_price == 0 path where the oracle reads LT.agg().price() itself.
"""
import math
import boa

# Minimal price() ABI - the crvUSD aggregator and the AMM's PRICE_ORACLE_CONTRACT
# both expose it. (LT.agg() returns the aggregator; AMM.PRICE_ORACLE_CONTRACT() the
# per-market CryptopoolLPOracle, whose price() == lp_price_ps * agg / 1e18.)
PRICE_ABI = """[
    {"name":"price","outputs":[{"type":"uint256"}],"inputs":[],"stateMutability":"view","type":"function"},
    {"name":"AGG","outputs":[{"type":"address"}],"inputs":[],"stateMutability":"view","type":"function"}
]"""


def _price_at(addr):
    return boa.loads_abi(PRICE_ABI).at(addr).price()


def test_shared_aggregator_and_reconstruction(factory, net_pressure, lt_deployer,
                                              amm_deployer, twocrypto):
    """Every market uses the Factory's aggregator, and the AMM oracle price is exactly
    lp_price_ps * agg_price / 1e18 (the identity the in-contract reconstruction uses)."""
    factory_agg = factory.agg()
    agg_price = _price_at(factory_agg)
    # The aggregator must not be trivially 1.0, or this test wouldn't exercise the factor.
    assert agg_price != 10**18, agg_price

    assert factory.market_count() > 0
    for mid in range(factory.market_count()):
        market = factory.markets(mid)
        lt = lt_deployer.at(market.lt)
        amm = amm_deployer.at(market.amm)
        pool = twocrypto.at(market.cryptopool)

        # (1) shared aggregator: the LT's agg and the Factory's agg are the same contract.
        assert lt.agg().lower() == factory_agg.lower(), f"m{mid}: LT.agg != Factory.agg"

        # (2) reconstruction identity, on real pool state, with agg != 1.0.
        lp_price_ps = 2 * pool.virtual_price() * math.isqrt(pool.price_scale() * 10**18) // 10**18
        reconstructed = lp_price_ps * agg_price // 10**18
        poc_price = _price_at(amm.PRICE_ORACLE_CONTRACT())
        assert reconstructed == poc_price, f"m{mid}: {reconstructed} != {poc_price}"


def test_supplied_agg_price_matches_self_read(factory, net_pressure, lt_deployer):
    """net_pressure_and_tvl / net_pressure_oracle / half_tvl_oracle with the explicit
    shared agg_price are bit-for-bit identical to the agg_price == 0 (self-read) path."""
    agg_price = _price_at(factory.agg())

    for mid in range(factory.market_count()):
        lt = factory.markets(mid).lt

        npt_default = net_pressure.net_pressure_and_tvl(lt)
        npt_arg = net_pressure.net_pressure_and_tvl(lt, agg_price)
        assert npt_arg.net_pressure == npt_default.net_pressure, f"m{mid}: net differs"
        assert npt_arg.half_tvl == npt_default.half_tvl, f"m{mid}: half_tvl differs"

        assert net_pressure.net_pressure_oracle(lt, agg_price) == npt_default.net_pressure
        assert net_pressure.half_tvl_oracle(lt, agg_price) == npt_default.half_tvl
        # The single-value entry points' own self-read path must also agree.
        assert net_pressure.net_pressure_oracle(lt) == npt_default.net_pressure
        assert net_pressure.half_tvl_oracle(lt) == npt_default.half_tvl


def test_signals_sane_on_real_markets(factory, net_pressure, lt_deployer, amm_deployer):
    """Sanity on live positions: half_tvl > 0 and in the ballpark of the AMM's
    value_oracle equity, and net pressure is smaller in magnitude than the equity."""
    for mid in range(factory.market_count()):
        market = factory.markets(mid)
        amm = amm_deployer.at(market.amm)

        npt = net_pressure.net_pressure_and_tvl(market.lt)
        value_oracle = amm.value_oracle().value

        assert npt.half_tvl > 0, f"m{mid}: half_tvl == 0"
        # half_tvl marks at price_oracle (EMA), value_oracle at price_scale, so they
        # differ by the EMA lag; a loose band catches a gross regression, not the lag.
        assert 0.8 < npt.half_tvl / value_oracle < 1.2, f"m{mid}: half_tvl/value_oracle out of band"
        # Net pressure is a deviation signal; it should be far below the whole equity.
        assert abs(npt.net_pressure) < npt.half_tvl, f"m{mid}: |net| >= half_tvl"

        print(f"m{mid}: net_pressure={npt.net_pressure}  half_tvl={npt.half_tvl}  "
              f"value_oracle={value_oracle}  ratio={npt.half_tvl / value_oracle:.4f}")
