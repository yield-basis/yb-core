# pragma version 0.4.3

# =============================================================================
# StableSwap (n=2), D=1, fixed-point WAD=1e18
#
# Goal:
#   Given amplification A and target marginal price p_target = -dx/dy,
#   compute portfolio value in x-units:
#     V = x + p_target * y
#   where (x, y) lies on the StableSwap invariant.
#
# Notation:
#   A_eff := A_raw / A_PRECISION
#   y := y/D, with D=1 normalization
#   g(y) := p(y) - p_target
#
# 1) Invariant and x(y)
#   For n=2, D=1:
#     4*A_eff*(x + y) + 1 = 4*A_eff + 1/(4*x*y)
#   Rearranged:
#     4*A_eff*x^2 + (4*A_eff*(y-1) + 1)*x - 1/(4*y) = 0
#   With b1 = 4*A_eff*(y-1)+1:
#     x(y) = (-b1 + sqrt(b1^2 + 4*A_eff/y)) / (8*A_eff)
#
# 2) Marginal price p(y)
#   From implicit differentiation of F(x,y)=0:
#     p(y) = -dx/dy
#          = (4*A_eff + 1/(4*x*y^2)) / (4*A_eff + 1/(4*x^2*y))
#
# 3) Value at fixed y
#     V(y) = x(y) + p_target * y
#
# 4) Root equation solved by numeric method
#     g(y) = p(y) - p_target = 0
#   On the relevant branch p(y) is monotone decreasing in y, so bracketing works.
#
# 5) Mapping used by this implementation
#   b1   = WAD + (4*A_raw*(y - WAD))/A_PRECISION
#   rad2 = b1^2 + (4*A_raw*WAD^3)/(A_PRECISION*y)
#   x    = ((-b1 + sqrt(rad2)) * A_PRECISION) / (8*A_raw)
#   p    = ((4*A_raw*x/A_PRECISION + WAD^3/(4*y^2)) * WAD) /
#          (4*A_raw*x/A_PRECISION + WAD^3/(4*x*y))
#
# 6) Symmetry for p_target < 1
#   Solve reciprocal branch at p_inv ~= WAD^2 / p_target, then map back:
#     V(p_target) = p_target * V(p_inv)
#     (x, y) at p_target is (y, x) at p_inv.
#
# 7) Method used here: pure bisection on g(y)
#   Find y in bracket [lo, hi] such that:
#     p(lo) > p_target >= p(hi)
#   Update:
#     if p(mid) > p_target: lo = mid
#     else:                 hi = mid
#   Stop when relative error is small:
#     |p(mid)-p_target| / p_target <= 1 / PRICE_TOL_REL
#   or hi-lo <= 1, or iteration cap.
# =============================================================================
WAD: constant(uint256) = 10**18
WAD2: constant(uint256) = WAD * WAD
WAD3: constant(uint256) = WAD2 * WAD

A_PRECISION: constant(uint256) = 10**4
MAX_A: constant(uint256) = 100_000
MAX_A_RAW: constant(uint256) = MAX_A * A_PRECISION

BISECTION_ITERS: constant(uint256) = 64  # 2^64 < 10^19
PRICE_TOL_REL: constant(uint256) = 10**6  # 0.01 bps


@internal
@pure
def _x_from_y(A_raw: uint256, y: uint256) -> uint256:
    # Invariant quadratic in x:
    #   4A*x^2 + (4A*(y-1)+1)*x - 1/(4y) = 0
    # Positive root:
    #   x(y) = (-b1 + sqrt(b1^2 + 4A/y)) / (8A), b1 = 1 - 4A*(1-y)
    #
    # Error bound for fixed-point rounding (absolute, in output wei):
    #   x*     = ((sqrt(b^2 + t) - b) * A_PRECISION) / (8*A_raw)      (exact real)
    #   b      = WAD - (4*A_raw*(WAD-y))/A_PRECISION
    #   t      = (4*A_raw*WAD^3)/(A_PRECISION*y)
    #   b_hat  = WAD - floor(4*A_raw*(WAD-y)/A_PRECISION), |b_hat-b| < 1
    #   t_hat  = floor(t),                                    |t_hat-t| < 1
    #   r_hat  = floor(sqrt(b_hat^2 + t_hat))
    #   x_hat  = floor(((r_hat - b_hat) * A_PRECISION)/(8*A_raw))
    # Using |d sqrt(b^2+t)/db| <= 1 and |d sqrt(b^2+t)/dt| = 1/(2*sqrt(b^2+t)) << 1:
    #   |r_hat - sqrt(b^2+t)| < 2
    # Therefore:
    #   |x_hat - x*| < 1 + (3*A_PRECISION)/(8*A_raw)
    #   => for A_raw >= 1:            |x_hat - x*| < 3751 wei
    #   => for A_raw >= A_PRECISION:  |x_hat - x*| < 2 wei
    b1: int256 = convert(WAD, int256) - convert(4 * A_raw * (WAD - y) // A_PRECISION, int256)

    abs_b1: uint256 = convert(abs(b1), uint256)
    term: uint256 = unsafe_div(4 * A_raw * WAD3, A_PRECISION * y)
    rad: int256 = convert(isqrt(abs_b1**2 + term), int256)
    if rad <= b1:  # extra safety
        return 0

    return (convert(rad - b1, uint256) * A_PRECISION) // (8 * A_raw)


@internal
@pure
def _p_from_y(A_raw: uint256, y: uint256) -> uint256:
    # p(y) = -dx/dy:
    #   p(y) = (4A + 1/(4*x*y^2)) / (4A + 1/(4*x^2*y))
    # Multiply numerator and denominator by x to reduce one division by x:
    #   p(y) = (4A*x + 1/(4*y^2)) / (4A*x + 1/(4*x*y))
    #
    # Error propagation (absolute, in output wei):
    #   p*   = WAD * (N / D), with:
    #          N = a + u,  D = a + v
    #          a = (4*A_raw/A_PRECISION) * x*
    #          u = WAD^3/(4*y^2)
    #          v = WAD^3/(4*x* y)
    #   p_hat uses x_hat from _x_from_y and floor divisions.
    #
    # Let E_x = |x_hat - x*| from _x_from_y bound, alpha = 4*A_raw/A_PRECISION,
    # beta = WAD^3/(4*y). For x* > E_x:
    #   |delta_a| <= alpha * E_x + 1
    #   |delta_u| < 1
    #   |delta_v| <= beta * E_x / (x* * (x* - E_x)) + 1
    #
    # Define:
    #   deltaN = |delta_a| + |delta_u|
    #   deltaD = |delta_a| + |delta_v|
    # Then for deltaD < D:
    #   |p_hat - p*| <= 1 + WAD * (deltaN * D + N * deltaD) / (D * (D - deltaD))
    #
    # Equivalent relative form (first-order):
    #   |p_hat - p*| / p* ~= deltaN / N + deltaD / D + 1 / p*
    #
    # Empirical examples on solver domain y in [WAD/10^5, WAD/2+1]
    # (dense y-sweep, high-precision reference; illustrative, not a proof):
    #   A_eff = 1      (A_raw = 1 * A_PRECISION):       |p_hat - p*| <= ~6.3e3 wei
    #   A_eff = 200    (A_raw = 200 * A_PRECISION):     |p_hat - p*| <= ~4.1e3 wei
    #   A_eff = 10_000 (A_raw = 10_000 * A_PRECISION):  |p_hat - p*| <= ~1.3e4 wei
    #   Relative error in all those sweeps is about 1e-18.
    x: uint256 = self._x_from_y(A_raw, y)
    if x == 0:
        return max_value(uint256)

    term4A: uint256 = (4 * A_raw * x) // A_PRECISION
    return unsafe_div(
        (term4A + unsafe_div(WAD3, 4 * y * y)) * WAD,
        term4A + unsafe_div(WAD3, 4 * x * y),
    )


@internal
@pure
def _y_from_bisection(A_raw: uint256, p: uint256) -> uint256:
    # Solve g(y) = p(y) - p_target = 0 on monotone branch y in (0, 1/2].
    assert p >= WAD
    lo: uint256 = WAD // 10**5  # y for p ~ 5000 and A=100_000
    hi: uint256 = WAD // 2 + 1  # y for p = 1

    for _: uint256 in range(BISECTION_ITERS):
        mid: uint256 = unsafe_div(unsafe_add(lo, hi), 2)
        pm: uint256 = self._p_from_y(A_raw, mid)
        tol_abs: uint256 = unsafe_div(p, PRICE_TOL_REL)

        if pm > p:
            if unsafe_sub(pm, p) <= tol_abs:
                return mid
            lo = mid
        else:
            if unsafe_sub(p, pm) <= tol_abs:
                return mid
            hi = mid

        if unsafe_sub(hi, lo) <= 1:
            return hi

    raise "Didn't converge"  # Unreachable


@internal
@pure
def _get_x_y(A_raw: uint256, p: uint256) -> (uint256, uint256):
    assert A_raw > 0
    assert A_raw <= MAX_A_RAW
    assert p != 0

    if p < WAD:
        p_inv: uint256 = unsafe_div(WAD2 + p // 2, p)
        y_inv: uint256 = self._y_from_bisection(A_raw, p_inv)
        x_inv: uint256 = self._x_from_y(A_raw, y_inv)
        return y_inv, x_inv

    y: uint256 = self._y_from_bisection(A_raw, p)
    x: uint256 = self._x_from_y(A_raw, y)
    return x, y


@internal
@pure
def _portfolio_value(A_raw: uint256, p: uint256) -> uint256:
    x: uint256 = 0
    y: uint256 = 0
    x, y = self._get_x_y(A_raw, p)
    return x + p * y // WAD


@external
@pure
def portfolio_value(_A_raw: uint256, _p: uint256) -> uint256:
    return self._portfolio_value(_A_raw, _p)
