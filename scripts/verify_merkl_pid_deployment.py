#!/usr/bin/env python3
"""
Verify the deployed net-pressure Merkl PID stack against the deployment JSON, BEFORE the DAO
vote connects it. Read-only: forks the recorded node at head and only calls view functions,
so it never broadcasts anything.

Checks the wiring/ownership of every deployed contract, confirms the driver still carries the
report's design-point gains (§10 of docs/net_pressure/REPORT_dynamic_incentives.md), and
confirms the system is NOT yet connected (fee_receiver still the FeeDistributor,
driver.connected() == False, reserve empty, Merkl not wired). Then prints the live control
signals - net pressure, half-TVL, market rate, and the APR the driver would offer on a cold
connect - all in natural units.

    python scripts/verify_merkl_pid_deployment.py
"""
import os
import json
import warnings
import boa

# Attaching local contract sources to on-chain addresses (.at()) makes boa compare the freshly
# compiled bytecode against the deployed bytecode; they differ in immutables/metadata, which is
# harmless since we only read through the ABI. Silence just that one warning.
warnings.filterwarnings("ignore", message="casted bytecode does not match compiled bytecode",
                        category=UserWarning)

HERE = os.path.dirname(os.path.abspath(__file__))     # .../scripts
REPO = os.path.dirname(HERE)                           # repo root (for contract paths)
DEPLOY_JSON = os.path.join(HERE, "merkl_pid_deployment.json")

# The report's found design point (§10). d_filter_time is seconds; everything else is 1e18.
EXPECTED_GAINS = {
    "feedforward_gain": 1_160_000_000_000_000_000,     # 1.16
    "kp": 50 * 10**18,                                 # 50
    "ki": 1988 * 10**18,                               # 1988
    "kd": 49_000_000_000_000_000,                      # 0.049 (6h-filter-matched, not raw 0.0158)
    "max_integral": 2_930_000_000_000_000_000,         # 2.93
    "sink_cap": 22 * 10**18,                           # 22
    "dead_band": 1_600_000_000_000_000_000,            # 1.6
    "sink_per_offer": 500_000_000_000_000_000,         # 0.5
    "d_filter_time": 6 * 3600,                          # 6 h
}
EXPECTED_EXEC = {
    "swap_fee_multiplier": 3 * 10**18 // 2,            # 1.5x
    "dust_floor": 10**12,
}
ZAP_SLIPPAGE = 3 * 10**18 // 2                          # 1.5x on the LTSwapZap

AGG_ABI = json.dumps([
    {"name": "price", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
])
# The sink is the crvUSD/pyUSD stableswap pool. Merkl measures its TVL the same way we do here:
# totalSupply * get_virtual_price / 1e18 (crvUSD, 1e18).
SINK_POOL_ABI = json.dumps([
    {"name": "get_virtual_price", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "totalSupply", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint256"}]},
])
ERC20_ABI = json.dumps([
    {"name": "balanceOf", "stateMutability": "view", "type": "function", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "decimals", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}]},
])
SYMBOL_ABI = json.dumps([
    {"name": "symbol", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "string"}]},
])
WRAPPER_ABI = json.dumps([
    {"name": "token", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "holder", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "distributor", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
])
FACTORY_OWNER_ABI = json.dumps([
    {"name": "ADMIN", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
])
# Merkl's DistributionCreator exposes the live Distributor (the contract that pulls rewards on
# claim); the wrapper derives its own distributor() from the creator, so they must match.
DIST_CREATOR_ABI = json.dumps([
    {"name": "distributor", "stateMutability": "view", "type": "function", "inputs": [], "outputs": [{"type": "address"}]},
])


def cpath(rel):
    return os.path.join(REPO, rel)


class Report:
    """Collects PASS/FAIL lines so a single bad wire doesn't abort the whole scan."""
    def __init__(self):
        self.failures = 0

    def check(self, label, got, expected):
        ok = _norm(got) == _norm(expected)
        mark = "PASS" if ok else "FAIL"
        if not ok:
            self.failures += 1
        print(f"  [{mark}] {label}")
        if not ok:
            print(f"         got      {got}")
            print(f"         expected {expected}")
        return ok

    def note(self, label, value):
        print(f"  [ -- ] {label}: {value}")


def _norm(v):
    """Case-insensitive address compare; identity otherwise."""
    if isinstance(v, str) and v.startswith("0x"):
        return v.lower()
    return v


def _addr(x):
    return str(x)


def main():
    cfg = json.load(open(DEPLOY_JSON))
    network = cfg["network"]
    print(f"Verifying Merkl PID deployment from {DEPLOY_JSON}")
    print(f"Forking node {network} at head (read-only)\n")

    r = Report()
    with boa.fork(network):
        factory = boa.load_partial(cpath('contracts/Factory.vy')).at(cfg["factory"])
        driver = boa.load_partial(cpath('contracts/net_pressure/MerklPIDDriver.vy')).at(cfg["merkl_pid_driver"])
        fs = boa.load_partial(cpath('contracts/net_pressure/FeeSplitter.vy')).at(cfg["fee_splitter"])
        zap = boa.load_partial(cpath('contracts/utils/LTSwapZap.vy')).at(cfg["lt_swap_zap"])
        oracle = boa.load_partial(cpath('contracts/net_pressure/YBNetPressure.vy')).at(cfg["net_pressure_oracle"])
        mrate = boa.load_partial(cpath('contracts/net_pressure/MarketRateGetter.vy')).at(cfg["market_rate_getter"])
        wrapper = boa.loads_abi(WRAPPER_ABI).at(cfg["merkl_wrapper"])
        crvusd = boa.loads_abi(ERC20_ABI).at(cfg["crvusd"])

        expected_lts = [_addr(factory.markets(i).lt) for i in cfg["pressure_market_ids"]]
        # LT symbols (yb-<asset>) for labeling the pressure markets.
        lt_symbols = {mid: boa.loads_abi(SYMBOL_ABI).at(lt).symbol()
                      for mid, lt in zip(cfg["pressure_market_ids"], expected_lts)}

        # --- deployment addresses --------------------------------------------
        print("Deployment addresses")
        for label in ("deployer", "dao", "crvusd", "factory", "fee_distributor", "sink_lp",
                      "susds", "net_pressure_oracle", "market_rate_getter", "lt_swap_zap",
                      "merkl_pid_driver", "fee_splitter", "merkl_wrapper", "distribution_creator"):
            print(f"  {label:22s} ({cfg[label]})")
        print()

        # --- MerklPIDDriver: wiring, ownership, roles ------------------------
        print(f"MerklPIDDriver — wiring & ownership  ({cfg['merkl_pid_driver']})")
        r.check("owner == DAO", _addr(driver.owner()), cfg["dao"])
        r.check("manager == deployer", _addr(driver.manager()), cfg["deployer"])
        r.check("CRVUSD == crvusd", _addr(driver.CRVUSD()), cfg["crvusd"])
        r.check("FACTORY == factory", _addr(driver.FACTORY()), cfg["factory"])
        r.check("net_pressure == oracle", _addr(driver.net_pressure()), cfg["net_pressure_oracle"])
        r.check("market_rate_getter == mrate", _addr(driver.market_rate_getter()), cfg["market_rate_getter"])
        r.check("fee_distributor == fd", _addr(driver.fee_distributor()), cfg["fee_distributor"])
        r.check("sink_pool == sink_lp", _addr(driver.sink_pool()), cfg["sink_lp"])
        got_lts = [_addr(driver.pressure_lts(i)) for i in range(len(expected_lts))]
        r.check(f"pressure_lts == markets {cfg['pressure_market_ids']}",
                [a.lower() for a in got_lts], [a.lower() for a in expected_lts])

        # --- MerklPIDDriver: gains match the report design point -------------
        print("\nMerklPIDDriver — gains (report §10 design point)")
        for name, exp in EXPECTED_GAINS.items():
            r.check(f"{name} == {_nat(name, exp)}", getattr(driver, name)(), exp)
        print("\nMerklPIDDriver — execution params")
        for name, exp in EXPECTED_EXEC.items():
            r.check(f"{name} == {_nat(name, exp)}", getattr(driver, name)(), exp)

        # --- Not yet connected / not yet Merkl-wired (pre-vote invariants) ---
        print("\nPre-vote state (must NOT be connected yet)")
        r.check("driver.connected() is False", driver.connected(), False)
        r.check("factory.fee_receiver() still the FeeDistributor",
                _addr(factory.fee_receiver()), cfg["fee_distributor"])
        r.check("merkl_creator not wired (0x0)", _addr(driver.merkl_creator()),
                "0x0000000000000000000000000000000000000000")
        r.check("reward_wrapper not wired (0x0)", _addr(driver.reward_wrapper()),
                "0x0000000000000000000000000000000000000000")
        r.check("reserve empty (not seeded yet)", driver.reserve(), 0)

        # --- FeeSplitter ------------------------------------------------------
        print(f"\nFeeSplitter  ({cfg['fee_splitter']})")
        r.check("pid == driver", _addr(fs.pid()), cfg["merkl_pid_driver"])
        r.check("fee_distributor == fd", _addr(fs.fee_distributor()), cfg["fee_distributor"])
        r.check("split_fraction == json", fs.split_fraction(), cfg["split_fraction"])
        r.check("owner == DAO", _addr(fs.owner()), cfg["dao"])

        # --- LTSwapZap --------------------------------------------------------
        print(f"\nLTSwapZap  ({cfg['lt_swap_zap']})")
        r.check("CRVUSD == crvusd", _addr(zap.CRVUSD()), cfg["crvusd"])
        r.check("NET_PRESSURE == oracle", _addr(zap.NET_PRESSURE()), cfg["net_pressure_oracle"])
        r.check("swap_fee_multiplier == 1.5x", zap.swap_fee_multiplier(), ZAP_SLIPPAGE)
        r.check("owner == DAO", _addr(zap.owner()), cfg["dao"])

        # --- MarketRateGetter -------------------------------------------------
        print(f"\nMarketRateGetter  ({cfg['market_rate_getter']})")
        r.check(f"SUSDS_TOKEN == susds ({cfg['susds']})", _addr(mrate.SUSDS_TOKEN()), cfg["susds"])

        # --- Merkl wrapper (Pull-on-Claim proxy) ------------------------------
        print(f"\nMerkl wrapper (Pull-on-Claim)  ({cfg['merkl_wrapper']})")
        creator = boa.loads_abi(DIST_CREATOR_ABI).at(cfg["distribution_creator"])
        r.check("token == crvusd", _addr(wrapper.token()), cfg["crvusd"])
        r.check("holder == driver", _addr(wrapper.holder()), cfg["merkl_pid_driver"])
        # distributor() is Merkl's Distributor (not the Creator); it must match the one the
        # DistributionCreator itself uses, which is what the wrapper was initialized against.
        r.check("distributor == DistributionCreator.distributor()",
                _addr(wrapper.distributor()), _addr(creator.distributor()))

        # --- Factory owner proxy (the vote's set_fee_receiver caller) ---------
        print("\nFactory owner proxy")
        factory_owner = boa.loads_abi(FACTORY_OWNER_ABI).at(factory.admin())
        r.check("factory owner ADMIN == DAO", _addr(factory_owner.ADMIN()), cfg["dao"])

        # ====================================================================
        # Live control signals, in natural units
        # ====================================================================
        print("\n" + "=" * 68)
        print("Live control signals (natural units)")
        print("=" * 68)

        agg_addr = _addr(factory.agg())
        agg = boa.loads_abi(AGG_ABI).at(agg_addr)
        agg_price = agg.price()
        print(f"  crvUSD aggregator ({agg_addr}) price: {agg_price/1e18:.6f}")

        print("\n  Per-market net pressure (crvUSD = debt - crvUSD in pool; +ve is bad):")
        total_net = 0
        total_half = 0
        for mid, lt in zip(cfg["pressure_market_ids"], expected_lts):
            pt = oracle.net_pressure_and_tvl(lt, agg_price)
            total_net += pt.net_pressure
            total_half += pt.half_tvl
            ratio = (pt.net_pressure / pt.half_tvl * 100) if pt.half_tvl else 0.0
            print(f"    market {mid:2d} {lt_symbols[mid]:9s} ({lt}): "
                  f"net={pt.net_pressure/1e18:>14,.2f}  half_tvl={pt.half_tvl/1e18:>14,.2f}  "
                  f"({ratio:+.3f}% of half-TVL)")

        agg_ratio = (total_net / total_half * 100) if total_half else 0.0
        print(f"\n  Aggregate net pressure      : {total_net/1e18:,.2f} crvUSD")
        print(f"  Aggregate half-TVL          : {total_half/1e18:,.2f} crvUSD  (== YB TVL)")
        print(f"  Net pressure ratio          : {agg_ratio:+.4f}% of half-TVL "
              f"({'shortfall — controller would act' if total_net > 0 else 'no shortfall (<=0 is healthy)'})")

        sig = driver.raw_signals()
        print(f"\n  raw_signals.pressure        : {sig.pressure/1e18*100:.4f}% of half-TVL "
              f"(floored at 0)")
        print(f"  raw_signals.half_tvl        : {sig.half_tvl/1e18:,.2f} crvUSD")
        print(f"  market rate (sUSDS SSR)     : {sig.market_rate/1e18*100:.3f}% APR")

        # The sink Merkl feeds back: the crvUSD/pyUSD pool's live TVL, measured as
        # totalSupply * get_virtual_price / 1e18 (crvUSD, 1e18). This is what makes the sink
        # non-zero, so the offered APR reflects real coverage rather than a cold start.
        sink_pool = boa.loads_abi(SINK_POOL_ABI).at(cfg["sink_lp"])
        sink_tvl = sink_pool.totalSupply() * sink_pool.get_virtual_price() // 10**18
        sink_frac = sink_tvl / sig.half_tvl if sig.half_tvl else 0.0
        print(f"\n  Sink pool ({cfg['sink_lp']}):")
        print(f"    measured TVL              : {sink_tvl/1e18:,.2f} crvUSD "
              f"(totalSupply * get_virtual_price)")
        print(f"    sink = TVL / half-TVL     : {sink_frac*100:.4f}%  "
              f"(vs pressure {sig.pressure/1e18*100:.4f}%)")

        # Realistic connect preview: fresh state (integral=0, d_pressure=0,
        # prev_pressure=pressure, dt=0) but with the REAL measured sink, so error = pressure -
        # sink is what the controller would actually see the moment Merkl connects.
        st = driver.preview_target_apr(sink_tvl, 0, sig.pressure, 0, 0)
        covered = sig.pressure <= st.sink
        print(f"\n  Connect preview (real sink, fresh state):")
        print(f"    error = pressure - sink   : {(st.pressure - st.sink)/1e18*100:+.4f}% of half-TVL "
              f"({'sink already covers pressure' if covered else 'shortfall remains'})")
        if sig.market_rate:
            print(f"    offered bonus APR         : {st.target_apr/1e18*100:.3f}%  "
                  f"(= {st.target_apr/sig.market_rate:.2f}x market, on top of base)")
        else:
            print(f"    offered bonus APR         : {st.target_apr/1e18*100:.3f}%")

        # --- initial seed lifetime at the current offer ----------------------
        # The reserve is seeded (via the vote) by recovering the deprecated markets' LT fees and
        # zapping them to crvUSD. Size that seed with the deploy script's authoritative sim, then
        # estimate how long it funds incentives at the CURRENT offer. Merkl pays the bonus APR on
        # the whole measured sink, so annual spend = offered_apr * sink_tvl.
        print(f"\n  Initial seed (recovered from deprecated markets {cfg['deprecated_market_ids']}):")
        try:
            import deploy_merkl_pid_system as dep
            seed = dep.simulate_seed(dep.load_contracts(cfg))
        except Exception as e:
            seed = None
            print(f"    seed sim unavailable        : {e}")
        if seed is not None:
            annual_spend = st.target_apr / 10**18 * (sink_tvl / 10**18)   # crvUSD/yr on the sink
            print(f"    seed size                 : {seed/1e18:,.0f} crvUSD")
            print(f"    incentive spend @ current : {annual_spend:,.0f} crvUSD/yr "
                  f"({st.target_apr/1e18*100:.2f}% APR on {sink_tvl/1e18:,.0f} crvUSD sink)")
            if annual_spend > 0:
                days = seed / 10**18 / annual_spend * 365
                print(f"    seed lasts at this rate   : ~{days:,.0f} days (~{days/30.44:.1f} months)")
                print(f"    NB: this is a worst-case burn - pressure is elevated now (tBTC). On the")
                print(f"        2.4-yr backtest average (~0.1%/yr of half-TVL) it lasts far longer, and")
                print(f"        the offer self-limits as the sink grows and pressure subsides.")
            else:
                print(f"    no bonus at current state -> seed does not deplete")

    print("\n" + "=" * 68)
    if r.failures == 0:
        print("RESULT: all checks PASSED — deployment matches the JSON and is correctly")
        print("        configured, pre-vote (not yet connected to the DAO fee route).")
    else:
        print(f"RESULT: {r.failures} check(s) FAILED — see above.")
    print("=" * 68)
    raise SystemExit(1 if r.failures else 0)


def _nat(name, wei):
    """Human-readable form of an expected param for the check label."""
    if name == "d_filter_time":
        return f"{wei/3600:g}h"
    return f"{wei/1e18:g}"


if __name__ == "__main__":
    main()
