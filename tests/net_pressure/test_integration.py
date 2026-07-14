"""
Full-stack integration for the net-pressure incentive system on a real YB market
(LT + AMM + Curve cryptopool). Uses small mocks only for the FeeDistributor (the
real one needs VE/vesting wiring) and the sink stableswap pool valuation.

Flow exercised:
  admin deposits -> LT shares ("fees") -> FeeSplitter.trigger()
    -> split fraction to PID, rest to FeeDistributor(mock).fill_epochs()
    -> PID converts its LT shares to crvUSD (LT.withdraw + cryptopool swap)
    -> PID controller sets the FastGauge crvUSD/sec rate from real net pressure
  staker deposits sink LP into FastGauge -> accrues -> claims crvUSD.
"""
import boa


def test_full_stack(
    cryptopool, yb_lt, yb_amm, collateral_token, stablecoin, accounts, admin,
    yb_allocated, seed_cryptopool, token_mock, factory,
    net_pressure, mrate_getter_deployer, fastgauge_deployer, pid_deployer,
    feesplitter_deployer, susds_mock, fd_mock, sink_mock,
):
    staker = accounts[4]

    # Deepen the pool and open a real YB position.
    whale = accounts[2]
    stablecoin._mint_for_testing(whale, 50 * 100_000 * 10**18)
    collateral_token._mint_for_testing(whale, 50 * 10**18)
    with boa.env.prank(whale):
        stablecoin.approve(cryptopool.address, 2**256 - 1)
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        cryptopool.add_liquidity([50 * 100_000 * 10**18, 50 * 10**18], 0)

    p = cryptopool.price_oracle()
    collateral_token._mint_for_testing(admin, 5 * 10**18)
    with boa.env.prank(admin):
        yb_lt.deposit(5 * 10**18, p * 5 // 10**18 * 10**18, 0)
        yb_lt.set_rate(0)

    # Create positive net pressure: push the crypto price down, settle the EMA.
    dumper = accounts[3]
    collateral_token._mint_for_testing(dumper, 30 * 10**18)
    with boa.env.prank(dumper):
        collateral_token.approve(cryptopool.address, 2**256 - 1)
        for _ in range(4):
            cryptopool.exchange(1, 0, 5 * 10**18, 0)
    for _ in range(30):
        boa.env.time_travel(1200)

    # --- deploy the system -------------------------------------------------
    oracle = net_pressure
    susds = susds_mock.deploy(1000000001121484774769253326)  # ~3.5% APR
    mrate = mrate_getter_deployer.deploy(susds.address)
    fd = fd_mock.deploy()
    fd.set_tokens([yb_lt.address])
    sink_lp = token_mock.deploy("sinkLP", "sLP", 18)
    sink_pool = sink_mock.deploy(10**21, 10**18)  # modest sink so error>0

    gauge = fastgauge_deployer.deploy("sink", "sink", sink_lp.address, stablecoin.address, admin)
    pid = pid_deployer.deploy(stablecoin.address, factory.address,
                              oracle.address, mrate.address, fd.address, admin)
    fraction = 15 * 10**16  # 15% to PID (self-funds the spend from the fee-generating markets)
    fs = feesplitter_deployer.deploy(fd.address, pid.address, fraction, admin)

    with boa.env.prank(admin):
        pid.set_pressure_lts([yb_lt.address])
        pid.set_gauge(gauge.address, sink_pool.address)
        pid.set_execution_params(3 * 10**18 // 2, 10**12)
        gauge.set_pid(pid.address)
        # Install the FeeSplitter as the Factory fee_receiver so PID._connected() is true and
        # the controller runs (the gate keeps it off until our splitter is the fee route).
        factory.set_fee_receiver(fs.address)

    net = oracle.net_pressure_oracle(yb_lt.address)
    assert net > 0, f"expected positive net pressure, got {net}"

    # Simulate fees arriving at the splitter: a small slice of LT shares (fees are
    # tiny vs pool depth, so the conversion swap has negligible price impact).
    lt_fee = yb_lt.balanceOf(admin) // 1000
    with boa.env.prank(admin):
        yb_lt.transfer(fs.address, lt_fee)

    # A staker stakes sink LP into the gauge (so staked_value > 0 -> rate > 0).
    sink_lp._mint_for_testing(staker, 10**21)
    with boa.env.prank(staker):
        sink_lp.approve(gauge.address, 2**256 - 1)
        gauge.deposit(10**21, staker)

    pid_crvusd_before = stablecoin.balanceOf(pid.address)
    fd_lt_before = yb_lt.balanceOf(fd.address)

    # --- the trigger --------------------------------------------------------
    boa.env.time_travel(seconds=7200)  # let dt elapse since PID deploy
    # fs is the Factory fee_receiver, so its trigger realizes admin fees to itself on top of
    # the synthetic transfer. Measure the total it will split (anchor: realize, then roll back).
    with boa.env.anchor():
        yb_lt.withdraw_admin_fees()
        total_fee = yb_lt.balanceOf(fs.address)
    fs.trigger()

    # Split: `fraction` of the total fee went to the PID (converted), the rest to the FeeDistributor.
    to_pid = total_fee * fraction // 10**18
    assert yb_lt.balanceOf(fd.address) - fd_lt_before == total_fee - to_pid
    assert fd.filled() == 1
    # PID converted its LT shares into a crvUSD reserve.
    pid_reserve = stablecoin.balanceOf(pid.address) - pid_crvusd_before
    assert pid_reserve > 0, "PID did not accumulate a crvUSD reserve"
    assert yb_lt.balanceOf(pid.address) == 0, "PID still holds unconverted LT"

    # Controller set a positive stream rate from the real net pressure.
    rate = gauge.reward_rate()
    assert rate > 0, "expected a positive reward rate under positive net pressure"

    # --- staker earns and claims crvUSD ------------------------------------
    boa.env.time_travel(seconds=3600)
    claimable = gauge.claimable_reward(staker)
    assert claimable > 0
    with boa.env.prank(staker):
        gauge.claim(staker)
    assert stablecoin.balanceOf(staker) > 0
    # The reward came out of the PID reserve.
    assert stablecoin.balanceOf(pid.address) < pid_crvusd_before + pid_reserve

    # --- a second trigger (a new tx) -------------------------------------------
    # yb_lt is in BOTH the fee set and pressure_lts, so trigger() caches its
    # net_pressure_and_tvl in transient storage during conversion and reuses it for the
    # controller (one lp_oracle_2 solve). The contract clears transient per tx; boa does
    # not between calls, so emulate a fresh tx here.
    boa.env.evm.vm.state.clear_transient_storage()
    lt_fee2 = yb_lt.balanceOf(admin) // 1000
    with boa.env.prank(admin):
        yb_lt.transfer(fs.address, lt_fee2)
    reserve_before2 = stablecoin.balanceOf(pid.address)
    boa.env.time_travel(seconds=7200)
    fs.trigger()
    assert fd.filled() == 2
    assert stablecoin.balanceOf(pid.address) > reserve_before2  # converted again
    assert gauge.reward_rate() > 0                              # rate refreshed
