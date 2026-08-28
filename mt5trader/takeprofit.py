"""The exit price: break-even, and break-even plus a target.

A trader clicking a spread wants one number back immediately — where do
I get out? — and it is not a matter of taste. It is arithmetic on
figures this system already has:

- **Break-even** is the level the OTHER side of the book must reach for
  the trade to be worth nothing. Entering long means lifting the offer
  and leaving on the bid, so the round turn of both legs' bid-ask is
  already inside the two prices being compared; what is left to recover
  is COMMISSION, both legs, both ends.
- **The target** is a percentage of the MARGIN the position ties up.
  Margin is what the trade actually costs to hold, and it is per
  account and per broker — so it is read from the terminals with
  `order_calc_margin` rather than guessed from notional.

Money becomes spread points through `k` — `spread_units x quantity`,
the one multiplier every conversion in this system uses. There is no
second one here.

Two rules kept from everywhere else:

- **Unmeasured is not zero.** No margin figure from the terminals, or
  no `k`, means no target: the box shows an em dash and says what is
  missing. A TP of 0.00 would read as "get out at break-even", which is
  a different instruction.
- **Nothing here places an order.** It is a price on the screen. No
  bracket is sent to the broker, nothing exits by itself, and a leg
  never carries a broker-side stop (spec §4).
"""

from .models import SpreadSide


def points(money, spread_units, quantity=1.0):
    """`money` expressed in spread points, or None if it cannot be."""
    if money is None or not spread_units or not quantity:
        return None
    return money / (float(spread_units) * float(quantity))


def commission(pair, settings, quantity=1.0):
    """Commission for one round turn of `quantity` spreads, in money."""
    from . import costs
    lots_a = (pair.clip_lots_a or 0.0) * float(quantity)
    lots_b = (pair.clip_lots_b or 0.0) * float(quantity)
    if not lots_a and not lots_b:
        return None
    return costs.mark_fees(lots_a, lots_b, settings)


def describe(pair, md, settings, margin_per_spread=None, quantity=1.0,
             spread_units=None):
    """Break-even and take-profit for both directions, in spread points.

    Returned as the LEVELS the market has to reach, on the side that
    would actually close the trade:

    - a long is entered at the offer and closed on the bid, so its exit
      levels are compared against the bid (`short_spread`);
    - a short is entered on the bid and closed at the offer.

    That is the same convention the ladder, the mark and the slippage
    report use. Anything else here would be a fourth definition of the
    spread on one screen.
    """
    body = {'break_even_buy': None, 'break_even_sell': None,
            'tp_buy': None, 'tp_sell': None,
            # The three figures the exit is BUILT from, published beside
            # it: the round turn the market charges, the commission the
            # broker charges, and the profit the target asks for. A
            # break-even with no workings is a number to be trusted or
            # not; with them it can be checked.
            'spread_width': None, 'commission': None,
            'target_money': None, 'target_pct': None,
            'margin_per_spread': margin_per_spread, 'note': None}
    if not md:
        body['note'] = 'no market'
        return body

    long_spread = md.get('long_spread')      # what a BUY pays
    short_spread = md.get('short_spread')    # what a SELL receives
    if long_spread is not None and short_spread is not None:
        # One round turn of both legs' bid-ask, in spread points. It is
        # already inside the two prices below; it is shown because it is
        # the cost of the trade the trader is about to do.
        body['spread_width'] = long_spread - short_spread
    fees = commission(pair, settings, quantity)
    body['commission'] = fees
    fee_points = points(fees, spread_units, quantity)
    if fee_points is None:
        fee_points = 0.0 if fees in (None, 0.0) else None

    if long_spread is not None and fee_points is not None:
        # Long: bought at the offer, leaves on the bid. Break-even is
        # the bid reaching the offer paid, plus commission.
        body['break_even_buy'] = long_spread + fee_points
    if short_spread is not None and fee_points is not None:
        body['break_even_sell'] = short_spread - fee_points

    pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
    body['target_pct'] = pct
    if not pct:
        body['note'] = ('set TP_TARGET_PCT_OF_MARGIN in Settings to see a '
                        'target beside break-even')
        return body
    if not margin_per_spread:
        body['note'] = ('the terminals have not priced the margin for one '
                        'spread yet — break-even stands, the target does not')
        return body

    target_money = float(pct) / 100.0 * float(margin_per_spread) \
        * float(quantity)
    target_points = points(target_money, spread_units, quantity)
    body['target_money'] = target_money
    if target_points is None:
        body['note'] = 'no spread_units yet, so money cannot become points'
        return body
    if body['break_even_buy'] is not None:
        body['tp_buy'] = body['break_even_buy'] + target_points
    if body['break_even_sell'] is not None:
        body['tp_sell'] = body['break_even_sell'] - target_points
    body['note'] = (f'{pct:g}% of {margin_per_spread:,.2f} margin per '
                    f'spread = {target_money:,.2f}, over commission')
    return body


def for_position(position, md, pair, settings, margin_per_spread=None):
    """The same two numbers for a position that is ON — anchored on the
    price it was actually ENTERED at, not on the current touch.

    A take-profit that moves with the market is not a take-profit.
    """
    if position is None or position.entry_spread is None:
        return None
    quantity = position.quantity or 1.0
    units = position.spread_units
    fees = commission(pair, settings, quantity)
    fee_points = points(fees, units, quantity) or 0.0
    target_points = 0.0
    pct = settings.get('TP_TARGET_PCT_OF_MARGIN')
    if pct and margin_per_spread:
        target_points = points(
            float(pct) / 100.0 * float(margin_per_spread) * float(quantity),
            units, quantity) or 0.0
    if position.side is SpreadSide.BUY:
        break_even = position.entry_spread + fee_points
        return {'break_even': break_even, 'tp': break_even + target_points,
                'side': 'BUY'}
    break_even = position.entry_spread - fee_points
    return {'break_even': break_even, 'tp': break_even - target_points,
            'side': 'SELL'}
