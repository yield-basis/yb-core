"""Predict the LiquidityGauge (market.staker) addresses that Votes 2-5 will
create, by forking mainnet and (a) verifying the CREATE-nonce pattern against
existing markets and (b) actually simulating the 4 add_market calls."""
import boa
from eth_utils import keccak, to_checksum_address
from networks import NETWORK

YB_FACTORY = "0x370a449FeBb9411c95bf897021377fe0B7D100c0"


def rlp_create(sender_hex, nonce):
    sender = bytes.fromhex(sender_hex[2:])
    addr_item = bytes([0x94]) + sender                  # 0x80 + 20
    if nonce == 0:
        nonce_item = bytes([0x80])
    elif nonce < 0x80:
        nonce_item = bytes([nonce])
    else:
        nb = nonce.to_bytes((nonce.bit_length() + 7) // 8, "big")
        nonce_item = bytes([0x80 + len(nb)]) + nb
    payload = addr_item + nonce_item
    assert len(payload) < 56
    return bytes([0xc0 + len(payload)]) + payload


def create_address(sender_hex, nonce):
    return to_checksum_address(keccak(rlp_create(sender_hex, nonce))[12:])


boa.fork(NETWORK)
factory = boa.load_partial("contracts/Factory.vy").at(YB_FACTORY)

mc = factory.market_count()
print(f"market_count = {mc}")
impls = {}
for slot in ["price_oracle_impl", "lt_impl", "amm_impl",
             "virtual_pool_impl", "staker_impl", "flash"]:
    try:
        impls[slot] = getattr(factory, slot)()
    except Exception as e:
        impls[slot] = f"n/a ({e})"
    print(f"  {slot:18s} = {impls[slot]}")

nonce = boa.env.evm.vm.state.get_nonce(bytes.fromhex(YB_FACTORY[2:]))
print(f"Factory account nonce = {nonce}\n")

# --- map every CREATE nonce to an existing-market contract -------------------
rev = {}
for n in range(1, nonce):
    rev[create_address(YB_FACTORY, n).lower()] = n

print("=== Existing markets: which Factory CREATE nonce each contract is at ===")
ROLES = ["price_oracle", "lt", "amm", "virtual_pool", "staker"]
for i in range(mc):
    m = factory.markets(i)
    row = []
    for role in ROLES:
        addr = getattr(m, role)
        n = rev.get(addr.lower())
        row.append(f"{role}={n}")
    print(f"  market {i}: " + "  ".join(row))

# --- predicted addresses for the 4 new markets ------------------------------
print(f"\n=== Predicted addresses for new markets {mc}..{mc + 3} "
      f"(starting at nonce {nonce}) ===")
labels = ["WBTC", "cbBTC", "tBTC", "WETH"]
predicted_gauges = []
for k in range(4):
    base = nonce + 5 * k
    gauge = create_address(YB_FACTORY, base + 4)
    predicted_gauges.append(gauge)
    print(f"  market {mc + k} ({labels[k]}):")
    for j, role in enumerate(ROLES):
        print(f"      {role:13s} nonce {base + j} -> {create_address(YB_FACTORY, base + j)}")

# --- ground-truth simulation: actually call add_market 4x -------------------
print("\n=== Simulating 4x Factory.add_market on the fork ===")
admin = factory.admin()
print(f"  Factory.admin() = {admin}")
# Reuse the cryptopools of the markets being replaced (markets 3..6); coins(0)
# is crvUSD for all of them, so add_market's checks pass. Init code does not
# affect CREATE addresses, so the duplicate pool is irrelevant to the result.
pools = [factory.markets(i).cryptopool for i in range(3, 7)]
sim_gauges = []
with boa.env.prank(admin):
    for k, pool in enumerate(pools):
        m = factory.add_market(pool, 10**16, 1, 0)   # fee, rate, debt_ceiling=0
        new_id = mc + k
        mk = factory.markets(new_id)
        sim_gauges.append(mk.staker)
        print(f"  add_market #{new_id} ({labels[k]}): "
              f"staker/gauge = {mk.staker}")

# --- compare ----------------------------------------------------------------
print("\n=== Result ===")
ok = True
for k in range(4):
    match = predicted_gauges[k].lower() == sim_gauges[k].lower()
    ok &= match
    print(f"  {labels[k]:6s} gauge: predicted {predicted_gauges[k]}  "
          f"simulated {sim_gauges[k]}  {'OK' if match else 'MISMATCH'}")
print("\n" + ("ALL MATCH — gauge addresses are deterministic and predictable."
              if ok else "MISMATCH — review assumptions."))
