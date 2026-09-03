"""How many lots each leg trades, and what one spread is worth.

## Why the hedge is not simply `L_A x beta`

The spread is `S = P_B - beta * P_A`. Hold L_A lots of leg A and L_B
lots of leg B with contract sizes C_A and C_B. For a short-spread
position (long A, short B) a price move gives

    P&L = dP_A * L_A*C_A  -  dP_B * L_B*C_B

and we want exactly `-dS * k` for a positive scale k:

    -dS * k = -dP_B * k + beta * dP_A * k

Matching coefficients gives `L_B*C_B = k` and `L_A*C_A = beta * k`, so

    L_A * C_A = beta * L_B * C_B     ->     L_B = L_A * C_A / (beta * C_B)

`L_B = L_A * beta` is WRONG. It is identical only at beta 1 with equal
contract sizes — which is the only configuration the stat-arb engine
ever ran in, so it hid for months. At beta 2 the correct hedge is HALF
leg A's lots; the naive rule trades double, turning a should-be-zero
move into a loss three times the intended size.

`k = L_B * C_B` is the dollars-per-1.00-of-spread multiplier, and it is
LEG B's units. Every conversion from spread to money uses this one `k`:
the ladder's per-tick value, the working order's notional, the
position's P&L, the fees. Four places each deriving their own is how
they drift apart.
"""

import math

#: How closely a MATCHED minimum pair has to hedge, and how far the
#: search may walk leg A up to get there.
MATCH_TOLERANCE = 0.02
MATCH_MAX_STEPS = 20


def round_step(volume, step, minimum=0.0, down=False):
    """Snap to a tradable volume — NEAREST by default.

    `down=True` where overshooting is genuinely unsafe: the hedge.
    """
    if step and step > 0:
        scaled = volume / step
        volume = (math.floor(scaled + 1e-9) if down
                  else math.floor(scaled + 0.5 + 1e-9)) * step
        volume = round(volume, 8)
    return volume if volume >= minimum - 1e-9 else 0.0


def hedge_lots(leg_a_lots, contract_a, contract_b, beta, step=0.0,
               minimum=0.0):
    """Leg B lots that hedge leg A: `L_A * C_A / (beta * C_B)`.

    Rounds DOWN. Leg A's own size is a target and nearest is the honest
    reading of it; the hedge is a quantity that must not overshoot. With
    leg B's step ten times leg A's, nearest would turn a wanted 0.05
    into 0.1 — a hedge twice the position it is hedging, net short the
    difference. Short is the recoverable error.
    """
    beta = float(beta or 1.0)
    if not leg_a_lots or not contract_a or not contract_b or beta == 0:
        return 0.0
    target = leg_a_lots * contract_a / (beta * contract_b)
    return round_step(target, step, minimum, down=True)


def spread_units(leg_b_lots, contract_b):
    """`k` — dollars per 1.00 of spread movement. Leg B's units."""
    if not leg_b_lots or not contract_b:
        return 0.0
    return leg_b_lots * contract_b


def notional(lots, contract_size, price):
    if not lots or not contract_size or not price:
        return 0.0
    return lots * contract_size * price


def matched_minimum_lots(min_a, min_b, step_a, step_b, beta=1.0,
                         contract_a=1.0, contract_b=1.0):
    """The smallest MATCHED pair both legs can actually trade.

    Each leg's own minimum is right for a single-leg test, but using
    both on a pair is not a hedge: on CFI the spot minimum is 0.01
    (1 oz) and the futures minimum is 0.1 (10 oz), so a spread built
    that way is 9 oz net short.

    "Smallest" alone is not enough either, because the steps are coarse.
    On XAGUSD (5,000/lot) against XAUUSD (100/lot) the floor is 0.02
    lots on leg A whose exact hedge is 0.0149 on leg B — 0.01 is 33%
    under and 0.02 is 33% over, while 0.04/0.03 is 0.4% out. So the
    floor is where the search STARTS: step leg A up until the hedge
    lands within MATCH_TOLERANCE, and give up after MATCH_MAX_STEPS
    rather than inflate a clip chasing a ratio the steps cannot express.

    This is the default click quantity (spec, decision 7), so the number
    the ladder offers is the number both legs can actually clear.
    """
    beta = float(beta or 1.0)
    step_a = step_a or 0.01
    step_b = step_b or 0.01
    contract_a = float(contract_a or 1.0)
    contract_b = float(contract_b or 1.0)
    min_a, min_b = min_a or 0.0, min_b or 0.0

    per_a = (contract_a / (beta * contract_b)) if (beta and contract_b) else 0.0
    if per_a <= 0:
        return round(min_a, 8), round(min_b, 8)

    # Leg A must clear its own minimum AND carry a leg B that clears
    # leg B's. Round UP, always — a minimum must never be undercut.
    floor_a = max(min_a, min_b / per_a)
    floor_a = math.ceil(floor_a / step_a - 1e-9) * step_a

    def hedge_for(lots_a):
        want = lots_a * per_a
        lots_b = math.floor(want / step_b + 0.5 + 1e-9) * step_b
        if lots_b < min_b - 1e-9:
            lots_b = math.ceil(min_b / step_b - 1e-9) * step_b
        return lots_b, want

    best = None
    for k in range(MATCH_MAX_STEPS):
        lots_a = floor_a + k * step_a
        lots_b, want = hedge_for(lots_a)
        error = abs(lots_b - want) / want if want else 1.0
        if best is None or error < best[2]:
            best = (lots_a, lots_b, error)
        if error <= MATCH_TOLERANCE:
            break
    lots_a, lots_b, _ = best
    return round(lots_a, 8), round(lots_b, 8)


def minimum_notional(contract_a, contract_b, price_a, price_b, beta,
                     min_a=0.0, min_b=0.0):
    """The smallest per-leg notional this PAIR can trade at all.

    The binding constraint is whichever leg needs more money, and it is
    usually leg B. Returning the number lets the ladder say "this pair
    needs at least $43,515 a leg" BEFORE the operator tries to trade
    under it, instead of "the hedge rounds to zero" after.
    """
    beta = float(beta or 1.0)
    if not contract_a or not contract_b or not price_a or not price_b:
        return None
    needs = [min_a * contract_a * price_a] if min_a else []
    if min_b:
        # Leg B's minimum expressed as the leg A notional producing it:
        # L_A = min_b * beta * C_B / C_A, so that notional is
        # L_A * C_A * P_A, and C_A cancels.
        needs.append(min_b * beta * contract_b * price_a)
    return max(needs) if needs else None


def clip_plan(pair, meta_a, meta_b, price_a, price_b, spreads=1.0):
    """Resolve one click into leg lots, or say why it cannot be.

    `pair.clip_lots_a` is what ONE spread means on leg A; everything
    else follows from the hedge arithmetic above. Returns the execution
    instruction and the display block together, because the ladder must
    show the derivation beside the quantity — "1 spread = 0.10 A /
    0.10 B, $10 per 1.00 of spread" (spec §11: a number with no unit is
    not checkable).

    `reason` is set when the click must be REFUSED. It is checked
    before either leg moves (spec §4: pre-check BOTH legs before
    resting EITHER).
    """
    beta = float(pair.hedge_ratio or 1.0)
    contract_a = float(meta_a.get('contract_size') or 0.0)
    contract_b = float(meta_b.get('contract_size') or 0.0)
    step_a = meta_a.get('volume_step') or 0.0
    step_b = meta_b.get('volume_step') or 0.0
    min_a = meta_a.get('volume_min') or 0.0
    min_b = meta_b.get('volume_min') or 0.0
    max_a = meta_a.get('volume_max') or 0.0
    max_b = meta_b.get('volume_max') or 0.0

    reason = None
    if not contract_a or not contract_b:
        return {'reason': 'contract sizes are not known yet — read them '
                          'from MT5 before sizing anything',
                'leg_a_lots': 0.0, 'leg_b_lots': 0.0, 'spread_units': 0.0}

    unit_a, unit_b = pair.clip_lots_a, pair.clip_lots_b
    if not unit_a:
        # Nothing configured at all: the smallest size both legs can
        # actually clear.
        unit_a, unit_b = matched_minimum_lots(
            min_a, min_b, step_a, step_b, beta, contract_a, contract_b)
    elif not unit_b:
        # Leg A IS configured and its hedge did not settle — the hedge
        # for it is under leg B's minimum. Falling back to the matched
        # floor here would quietly trade a size the trader did not ask
        # for, and on this desk's broker that floor is TEN TIMES the
        # smallest leg A. Refused, with the number that would fix it.
        return {'reason': (
            f'{pair.clip_lots_a:g} lots of leg A hedges with less than leg '
            f"B's {min_b:g}-lot minimum, so one spread cannot be built at "
            f'that size. Raise Lots/spread A, or clear it to go back to '
            f'the smallest size both legs can do'),
            'spreads': float(spreads or 0.0),
            'leg_a_lots': 0.0, 'leg_b_lots': 0.0, 'spread_units': 0.0}

    spreads = float(spreads or 0.0)
    lots_a = round_step(unit_a * spreads, step_a, min_a)
    lots_b = round_step(unit_b * spreads, step_b, min_b, down=True)

    floor = minimum_notional(contract_a, contract_b, price_a, price_b,
                             beta, min_a, min_b)
    if lots_a <= 0:
        reason = (f"{spreads:g} spread(s) is {unit_a * spreads:g} lots on "
                  f"leg A, under its {min_a:g}-lot minimum")
    elif lots_b <= 0:
        reason = (f"the hedge for {lots_a:g} lots on leg A is under leg B's "
                  f"{min_b:g}-lot minimum"
                  + (f' — this pair needs at least ${floor:,.0f} a leg'
                     if floor else ''))
    elif max_a and lots_a > max_a + 1e-9:
        reason = (f"leg A wants {lots_a:g} lots but the broker's maximum "
                  f"is {max_a:g} — the order would be rejected")
    elif max_b and lots_b > max_b + 1e-9:
        # Never discovered AFTER leg A has filled. In the stat-arb
        # system an inverted beta sized leg B at 5,167 lots of gold —
        # $2.25bn — and the plan reported it as fine; MT5 would have
        # rejected it with 10014 after leg A was already on.
        reason = (f"the hedge wants {lots_b:g} lots on leg B but the "
                  f"broker's maximum is {max_b:g}. Check the hedge ratio "
                  f"{beta:g}: leg B is leg A x contract A / (beta x "
                  f"contract B), so a beta that is too SMALL inflates it.")

    k = spread_units(lots_b, contract_b)
    return {
        'reason': reason,
        'spreads': spreads,
        'leg_a_lots': lots_a, 'leg_b_lots': lots_b,
        'leg_a_contract': contract_a, 'leg_b_contract': contract_b,
        'leg_a_units': lots_a * contract_a, 'leg_b_units': lots_b * contract_b,
        'leg_a_notional_usd': notional(lots_a, contract_a, price_a),
        'leg_b_notional_usd': notional(lots_b, contract_b, price_b),
        'hedge_ratio': beta,
        #: `k` — the ONE multiplier. Everything money-valued reads this.
        'spread_units': k,
        'min_notional_usd': floor,
        'unit_lots_a': unit_a, 'unit_lots_b': unit_b,
        # The sentence the ladder prints beside the quantity box.
        'derivation': (f"{spreads:g} spread = {lots_a:g} A / {lots_b:g} B, "
                       f"${k:,.2f} per 1.00 of spread"),
    }
