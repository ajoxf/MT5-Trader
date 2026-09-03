"""LIMIT mode: quote one leg, cross the other.

A synthetic working order is backed by a REAL pending limit on ONE leg
(the "quoting" leg). When that limit fills, the other leg is crossed at
market IMMEDIATELY. This earns one leg's bid-ask and pays the other's
instead of paying both, and it buys that edge by taking on legging risk.

The part that is easy to get wrong: **the quoting leg's price is not
fixed.** The trader clicked a SPREAD level, but the pending lives on a
LEG, and the price that produces the clicked spread depends on where
the other leg is right now:

    quoting B, SELL the spread at S:   P_B = S + beta * ask_A
    quoting B, BUY  the spread at S:   P_B = S + beta * bid_A
    quoting A, SELL the spread at S:   P_A = (bid_B - S) / beta   (buy A)
    quoting A, BUY  the spread at S:   P_A = (ask_B - S) / beta   (sell A)

So the pending is re-priced to hold the implied spread at the clicked
level — chasing the OTHER leg, for the order's whole life. In the
stat-arb system a peg anchored on its own leg's book chased the market
away from itself, never filled, timed out at 15s and crossed: 13.96
seconds from click to pair-on against 24ms for the market path, and
+0.4700 of slippage.

Three more rules, each of which has a test:

1. **Re-peg on a dead band, not on every tick.** Every MODIFY loses
   queue position; re-pricing three times a second guarantees you are
   never at the front of a queue, which defeats the entire point.
2. **Re-peg by MODIFY, never cancel-and-replace** — one ticket for the
   order's life. (MT5 cannot change a pending's VOLUME by MODIFY, so a
   RESIZE is the one case that must cancel and replace, and it says so.)
3. **Every MODIFY can race a fill.** Check for the fill BEFORE
   modifying, or you re-price an order that has already become a naked
   position.
"""

import logging

from . import sizing
from .executor import slippage
from .models import (LegFill, OrderState, OrderType, SpreadPosition,
                     SpreadSide, new_id)


class QuoteGroup:
    """The real pending behind one (pair, side, level).

    Three clicks at 58.40 are three synthetics and ONE pending at the
    summed size. Cancelling one re-sizes it; cancelling the last pulls
    it. The synthetics stay individually cancellable — this is an
    aggregation, not a replacement for them.
    """

    def __init__(self, pair_key, side, level, leg, position_id=None):
        self.pair_key = pair_key
        #: Set when this group CLOSES a position (AutoRouting, spec
        #: 5.4). Its real pending carries `position=<ticket>`, so
        #: executing it closes rather than opening — on a hedging
        #: account a plain opposite pending would open a second
        #: position.
        self.position_id = position_id
        self.side = SpreadSide(side)
        self.level = float(level)
        self.leg = leg                 # 'a' or 'b' — which leg quotes
        self.ticket = None
        self.price = None              # where the pending is resting
        self.volume = 0.0              # lots resting on the quoting leg
        self.repegs = 0
        self.reason = None             # in the broker's own words
        self.orders = []               # the synthetics behind it

    @property
    def quantity(self):
        """Spreads still working across this group's synthetics."""
        return sum(o.remaining for o in self.orders if o.is_working)

    @property
    def closing(self):
        return self.position_id is not None

    def to_dict(self):
        return {'pair_key': self.pair_key, 'side': self.side.value,
                'level': self.level, 'leg': self.leg.upper(),
                'position_id': self.position_id,
                'intent': 'CLOSE' if self.closing else 'OPEN',
                'ticket': self.ticket, 'price': self.price,
                'volume': self.volume, 'repegs': self.repegs,
                'quantity': self.quantity, 'reason': self.reason,
                'orders': [o.order_id for o in self.orders]}


def quoting_leg(pair):
    """Which leg rests the pending.

    Default: the leg with the WIDER bid-ask — that is the spread being
    earned. The tension is real and the ladder shows both widths so the
    choice is made from measurement: the wider leg is usually the less
    liquid one, where you queue longest and fill least.
    """
    if pair.quoting_leg in ('a', 'b'):
        return pair.quoting_leg
    width_a = (pair.meta_a or {}).get('width') or 0.0
    width_b = (pair.meta_b or {}).get('width') or 0.0
    return 'b' if width_b >= width_a else 'a'


def peg_price(pair, md, side, level, leg):
    """The quoting leg's price that makes the spread equal `level`.

    Returns (price, order side on that leg). Anchored on the OTHER
    leg's touch — the whole point of the module.
    """
    beta = float(pair.hedge_ratio or 1.0)
    side = SpreadSide(getattr(side, 'value', side))
    leg_a_side, leg_b_side = side.leg_sides()
    if leg == 'b':
        # The other leg is A, and we cross it when this fills — so the
        # anchor is the touch we would PAY on A.
        anchor = md['leg_a_ask'] if side is SpreadSide.SELL else md['leg_a_bid']
        return level + beta * anchor, leg_b_side
    anchor = md['leg_b_bid'] if side is SpreadSide.SELL else md['leg_b_ask']
    return (anchor - level) / beta, leg_a_side


def implied_spread(pair, md, side, leg, price):
    """What spread a pending resting at `price` currently implies."""
    beta = float(pair.hedge_ratio or 1.0)
    side = SpreadSide(getattr(side, 'value', side))
    if leg == 'b':
        anchor = md['leg_a_ask'] if side is SpreadSide.SELL else md['leg_a_bid']
        return price - beta * anchor
    anchor = md['leg_b_bid'] if side is SpreadSide.SELL else md['leg_b_ask']
    return anchor - beta * price


class Quoter:
    """Holds every LIMIT-mode group and works them each poll."""

    def __init__(self, config, legs, executor, book):
        self.config = config
        self.legs = legs
        self.executor = executor
        self.book = book
        self.groups = {}              # (pair_key, side, level) -> QuoteGroup
        #: Fill -> hedge-on times, in ms. The number that says whether
        #: the 2.0s deadline is right (spec §4, §16).
        self.hedge_times = []
        #: The last market seen per pair, so an auto-close can be marked
        #: at the touch it was decided on rather than at nothing.
        self._markets = {}

    # -- the ladder's side of it -------------------------------------------

    def key_for(self, order):
        # The position is PART of the key. Aggregating an auto-TP close
        # into an entry order at the same level and side would silently
        # change what one of them means (spec 5.4).
        return (order.pair_key, order.side.value, round(order.level, 10),
                order.position_id)

    def group_for(self, pair, order):
        key = self.key_for(order)
        group = self.groups.get(key)
        if group is None:
            group = QuoteGroup(order.pair_key, order.side, order.level,
                               quoting_leg(pair),
                               position_id=order.position_id)
            self.groups[key] = group
        if order not in group.orders:
            group.orders.append(order)
        return group

    def cancel(self, order):
        """Pull one synthetic. The real pending is re-sized (or removed)
        on the next pass, which is also where a fill that raced the
        cancel is caught."""
        self.book.cancel(order.order_id)
        return self.groups.get(self.key_for(order))

    # -- the loop ----------------------------------------------------------

    def work(self, pair, md):
        """One pass over this pair's groups. Returns what happened.

        Order matters: FILLS first (a MODIFY can race one), then
        placement and re-pegging.
        """
        events = []
        self._markets[pair.key] = md
        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            if group.ticket is not None:
                event = self._check_fill(pair, md, group)
                if event:
                    events.append(event)
                    if key not in self.groups:
                        # A rejected hedge pulls the whole pair; there is
                        # nothing left here to re-rest.
                        continue
                    # Otherwise fall through: a PARTIAL fill leaves
                    # clicks still working, and they re-rest on this same
                    # pass rather than a poll later.
            if not group.quantity:
                if group.ticket is not None:
                    self._pull(pair, group, 'the last synthetic at this level '
                                            'was cancelled')
                    events.append({'group': key, 'action': 'pulled'})
                self.groups.pop(key, None)
                continue
            event = self._rest_or_repeg(pair, md, group)
            if event:
                events.append(event)
        return events

    def _wanted_volume(self, pair, group):
        """Lots on the quoting leg for the spreads still working here."""
        if group.closing:
            # A closing order is sized to WHAT ACTUALLY FILLED, not to
            # the pair's clip: a partially filled entry gets a partially
            # sized take-profit, and anything else would either leave a
            # remainder on or close more than is there.
            position = self.book.position(group.position_id)
            if position is None:
                return 0.0
            fill = position.leg_a if group.leg == 'a' else position.leg_b
            return fill.volume if fill else 0.0
        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        plan = sizing.clip_plan(pair, meta_a, meta_b, meta_a.get('mid'),
                                meta_b.get('mid'), group.quantity)
        if plan.get('reason'):
            group.reason = plan['reason']
            return 0.0
        return plan['leg_a_lots'] if group.leg == 'a' else plan['leg_b_lots']

    def _rest_or_repeg(self, pair, md, group):
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        symbol = pair.symbol_a if group.leg == 'a' else pair.symbol_b
        if leg is None:
            group.reason = f'no leg runner for {symbol}'
            return None

        # A synthetic must not rest on a stale or desynced print: the
        # level is still there when the quote refreshes, and if it is
        # not then it was never offered.
        if md.get('guard_reason'):
            group.reason = f"{md['guard_reason']} — holding off"
            return None

        price, order_side = peg_price(pair, md, group.side, group.level,
                                      group.leg)
        volume = self._wanted_volume(pair, group)
        if volume <= 0:
            return None

        if group.ticket is None:
            result = leg.place_limit(
                symbol, order_side.value, volume, price,
                comment=new_id('TP' if group.closing else 'LADDER'),
                # THE line that makes an auto-TP a close: with a
                # position on the request, executing it closes that
                # position. Without it, on a hedging account, it would
                # open a second one facing the other way.
                position_ticket=self._closing_ticket(group))
            if not result.get('ok'):
                group.reason = result.get('error')
                for order in group.orders:
                    if order.is_working:
                        order.state = OrderState.REJECTED
                        order.reason = result.get('error')
                return {'group': self.key_for(group.orders[0]),
                        'action': 'rejected', 'reason': result.get('error')}
            group.ticket = result['ticket']
            group.price = result.get('price', price)
            group.volume = volume
            # The broker's stops level can make the required peg
            # unreachable. Say so in words, on the ladder — not in a log
            # file (spec §4, §11).
            group.reason = result.get('price_note')
            for order in group.orders:
                order.pending_ticket = group.ticket
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'placed', 'ticket': group.ticket,
                    'price': group.price}

        if abs(volume - group.volume) > 1e-9:
            # MT5 cannot change a pending's VOLUME by MODIFY, so the one
            # case that must cancel and replace is a re-size — and only
            # a re-size. Prices always MODIFY.
            return self._resize(pair, leg, symbol, group, order_side, price,
                                volume)

        drift = self._drift(pair, md, group)
        band = self._dead_band(pair)
        if drift is None or drift <= band:
            return None
        result = leg.modify_order(group.ticket, price)
        if not result.get('ok'):
            group.reason = result.get('error')
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'repeg_failed', 'reason': result.get('error')}
        group.price = price
        group.repegs += 1
        return {'group': (group.pair_key, group.side.value, group.level,
                          group.position_id),
                'action': 'repegged', 'price': price, 'drift': drift}

    def _closing_ticket(self, group):
        """The broker ticket this group's pending must CLOSE, or None."""
        if not group.closing:
            return None
        position = self.book.position(group.position_id)
        if position is None:
            return None
        fill = position.leg_a if group.leg == 'a' else position.leg_b
        tickets = (fill.position_tickets if fill else None) or []
        return tickets[0] if tickets else None

    def _drift(self, pair, md, group):
        """How far the resting pending's implied spread has wandered."""
        if group.price is None:
            return None
        return abs(implied_spread(pair, md, group.side, group.leg,
                                  group.price) - group.level)

    def _dead_band(self, pair):
        increment = pair.effective_increment() or 0.0
        ticks = float(self.config.get('REPEG_DEAD_BAND_TICKS', 1.0) or 0.0)
        return increment * ticks

    def _resize(self, pair, leg, symbol, group, order_side, price, volume):
        cancel = leg.cancel_order(group.ticket)
        if cancel.get('filled_volume'):
            # It filled while we were re-sizing it. That is a fill, not a
            # cancel, and it must be hedged rather than tidied away.
            return self._on_fill(pair, group, cancel)
        result = leg.place_limit(
            symbol, order_side.value, volume, price,
            comment=new_id('TP' if group.closing else 'LADDER'),
            position_ticket=self._closing_ticket(group))
        if not result.get('ok'):
            group.ticket = None
            group.reason = result.get('error')
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'resize_failed', 'reason': result.get('error')}
        group.ticket = result['ticket']
        group.price = result.get('price', price)
        group.volume = volume
        for order in group.orders:
            order.pending_ticket = group.ticket
        return {'group': (group.pair_key, group.side.value, group.level,
                          group.position_id),
                'action': 'resized', 'volume': volume,
                'ticket': group.ticket}

    def _pull(self, pair, group, reason):
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        if leg is None or group.ticket is None:
            return None
        state = leg.cancel_order(group.ticket)
        group.ticket = None
        group.reason = reason
        if state.get('filled_volume'):
            # A cancel that did not prevent a fill is a distinct event
            # and must stay visible, not be smoothed into a clean pull.
            logging.critical("%s: pending at %s FILLED as it was cancelled — "
                             "hedging it", pair.key, group.level)
            return self._on_fill(pair, group, state)
        return state

    def _check_fill(self, pair, md, group):
        """Read the pending BEFORE touching it. Every MODIFY can race a
        fill, and `positions_get` shows the fill (carrying the ORDER's
        ticket) before deal history does."""
        leg = self.legs.get(pair.account_a if group.leg == 'a'
                            else pair.account_b)
        if leg is None:
            return None
        state = leg.order_state(group.ticket)
        if not state.get('filled_volume'):
            return None
        return self._on_fill(pair, group, state)

    def _on_fill(self, pair, group, state):
        """The quoting leg filled. Cross the other leg NOW.

        A pending can fill PARTIALLY: hedge to what actually filled, not
        to what rested.
        """
        filled = float(state.get('filled_volume') or 0.0)
        started = self.executor.clock()
        ticket, group.ticket = group.ticket, None
        if group.closing:
            return self._on_closing_fill(pair, group, state, filled, ticket)
        if state.get('still_open') and ticket is not None:
            # A PARTIAL fill leaves the rest of the pending resting.
            # Pull it before hedging: the residual is re-rested at the
            # next pass at the size that is actually still working, and
            # leaving it would put a second pending on the same level.
            leg = self.legs.get(pair.account_a if group.leg == 'a'
                                else pair.account_b)
            if leg is not None:
                rest = leg.cancel_order(ticket)
                extra = float(rest.get('filled_volume') or 0.0) - filled
                if extra > 0:
                    # It filled further while we were cancelling it.
                    filled += extra
                    state = dict(state, price=rest.get('price') or
                                 state.get('price'),
                                 position_tickets=rest.get('position_tickets')
                                 or state.get('position_tickets'))

        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        beta = float(pair.hedge_ratio or 1.0)
        contract_a = float(meta_a.get('contract_size') or 0.0)
        contract_b = float(meta_b.get('contract_size') or 0.0)
        leg_a_side, leg_b_side = group.side.leg_sides()

        if group.leg == 'b':
            cross_leg, cross_side = 'a', leg_a_side
            # L_A = beta * L_B * C_B / C_A — the same arithmetic as
            # everywhere else, read the other way round.
            cross_volume = sizing.round_step(
                filled * beta * contract_b / contract_a,
                meta_a.get('volume_step'), meta_a.get('volume_min'), down=True)
            quote_side = leg_b_side
        else:
            cross_leg, cross_side = 'b', leg_b_side
            cross_volume = sizing.hedge_lots(
                filled, contract_a, contract_b, beta,
                meta_b.get('volume_step'), meta_b.get('volume_min'))
            quote_side = leg_a_side

        cross = self.executor._cross_with_deadline(
            pair, cross_leg, cross_side, cross_volume,
            contract_a if cross_leg == 'a' else contract_b, new_id('HEDGE'))
        elapsed_ms = (self.executor.clock() - started) * 1000.0
        self.hedge_times.append(elapsed_ms)

        tickets = list(state.get('position_tickets') or [])
        quote_fill = {'ok': True, 'filled_volume': filled,
                      'price': state.get('price'),
                      'position_tickets': tickets,
                      'ticket': tickets[0] if tickets else None}

        if not cross.get('ok'):
            # Only a REJECTED crossing leg unwinds — and it unwinds by
            # ticket, on a hedging account.
            reason = (f"the hedge was rejected: {cross.get('error')} — "
                      f"unwinding leg {group.leg.upper()}")
            logging.critical("%s: %s", pair.key, reason)
            self.executor._unwind_leg(pair, group.leg, quote_side, quote_fill)
            # If the crossing account cannot trade, none of the remaining
            # synthetics on this pair can complete either.
            self._pull_pair(pair, reason)
            for order in group.orders:
                if order.is_working:
                    order.state = OrderState.REJECTED
                    order.reason = reason
            return {'group': (group.pair_key, group.side.value, group.level,
                              group.position_id),
                    'action': 'hedge_rejected', 'reason': reason}

        fills = ({'a': cross, 'b': quote_fill} if group.leg == 'b'
                 else {'a': quote_fill, 'b': cross})
        position = self._book_fill(pair, group, fills, contract_a, contract_b,
                                   elapsed_ms)
        self._settle_orders(group, position.quantity)
        return {'group': (group.pair_key, group.side.value, group.level,
                          group.position_id),
                'action': 'filled', 'position': position.position_id,
                'hedge_ms': elapsed_ms}

    def _on_closing_fill(self, pair, group, state, filled, ticket):
        """An AutoRouting take-profit filled on the quoting leg.

        That leg is already flat — the pending carried
        `position=<ticket>`, so executing it CLOSED it. The other leg is
        still on, and until it is closed this is an outright position:
        it goes now, by ticket, at market.
        """
        position = self.book.position(group.position_id)
        self.groups.pop(self.key_for_group(group), None)
        if position is None:
            # The position went while the order was resting. That is the
            # orphan-pending case with a guaranteed fill, and it has now
            # happened: say so loudly rather than booking a close of
            # something that is not there.
            logging.critical(
                '%s: a take-profit filled for position %s, which is no '
                'longer in the book. Check the account by hand.',
                pair.key, group.position_id)
            return {'group': self.key_for_group(group),
                    'action': 'tp_orphaned', 'position': group.position_id}
        # Shaped exactly like `close_tickets`' answer, so the exit
        # price is volume-weighted off the SAME structure however the
        # close was sent.
        quote_result = {'ok': True, 'closed': [
            {'ticket': ticket, 'volume': filled, 'price': state.get('price')}]}
        answer = self.executor.close_other_leg(
            pair, position, group.leg, quote_result,
            md=self.market_for(pair), reason='auto take-profit')
        for order in group.orders:
            if order.is_working:
                order.filled_quantity = order.quantity
                order.state = OrderState.FILLED
        return {'group': self.key_for_group(group), 'action': 'tp_filled',
                'position': position.position_id, 'ok': answer.get('ok')}

    def key_for_group(self, group):
        return (group.pair_key, group.side.value, round(group.level, 10),
                group.position_id)

    def market_for(self, pair):
        """The pair's last market, for marking a close. Set by `work`."""
        return self._markets.get(pair.key)

    def arm(self, pair, position, level, quantity=None, armed_by='auto'):
        """Rest a working order to CLOSE `position` at `level`.

        This is what AutoRouting does on a fill, and what the trader
        does by hand with Close @ Limit. Neither is a strategy: it
        places the exit order the trader would otherwise place a click
        at a time. No signal, no re-entry, no loop.

        It arms a TARGET and NO STOP — the position runs until the
        target, the overnight rule, or the trader.

        `armed_by` is WHO asked. It decides one thing only, and it
        matters: switching AutoRouting off sweeps what AutoRouting
        armed, and must not touch an exit the trader rested by hand.
        """
        if level is None:
            return None
        existing = self.book.orders_for_position(position.position_id)
        if existing:
            return existing[0]
        order = self.book.add_order(
            pair, position.side.opposite, level,
            quantity if quantity is not None else position.quantity,
            order_type=OrderType.LIMIT,
            position_id=position.position_id, armed_by=armed_by)
        self.group_for(pair, order)
        logging.info('%s: %s armed a %s to close %s at %s', pair.key,
                     'AutoRouting' if armed_by == 'auto' else 'the trader',
                     position.side.opposite.value,
                     position.position_id, level)
        return order

    def disarm(self, position_id, reason='its position is gone',
               armed_by=None):
        """Pull every closing order armed against one position.

        Called BEFORE the close, never after: an auto-TP left resting
        after its position is gone is the orphan-pending incident with a
        GUARANTEED fill — it will execute, and with nothing to close it
        opens a naked position instead.
        """
        pulled = []
        for order in self.book.orders_for_position(position_id,
                                                   armed_by=armed_by):
            self.book.cancel(order.order_id, reason)
            pulled.append(order)
        if armed_by is not None and not pulled:
            return pulled
        for key, group in list(self.groups.items()):
            if group.position_id != position_id:
                continue
            if armed_by is not None and any(
                    o.is_working for o in group.orders):
                # Something the caller was not asked to pull is still
                # working here: the real pending stays, re-sized on the
                # next pass to what is left.
                continue
            pair = self.config.pairs.get(group.pair_key)
            if pair is not None:
                self._pull(pair, group, reason)
            self.groups.pop(key, None)
        return pulled

    def _pull_pair(self, pair, reason):
        for key, group in list(self.groups.items()):
            if group.pair_key != pair.key:
                continue
            self._pull(pair, group, reason)
            for order in group.orders:
                if order.is_working:
                    order.state = OrderState.CANCELLED
                    order.reason = reason
            self.groups.pop(key, None)

    def _book_fill(self, pair, group, fills, contract_a, contract_b,
                   elapsed_ms):
        leg_a_side, leg_b_side = group.side.leg_sides()
        leg_a = LegFill(pair.account_a, pair.symbol_a, leg_a_side,
                        fills['a'].get('filled_volume') or 0.0,
                        fills['a'].get('price'),
                        order_ticket=fills['a'].get('ticket'),
                        position_tickets=fills['a'].get('position_tickets'),
                        contract_size=contract_a, clock=self.executor.clock)
        leg_b = LegFill(pair.account_b, pair.symbol_b, leg_b_side,
                        fills['b'].get('filled_volume') or 0.0,
                        fills['b'].get('price'),
                        order_ticket=fills['b'].get('ticket'),
                        position_tickets=fills['b'].get('position_tickets'),
                        contract_size=contract_b, clock=self.executor.clock)
        entry_spread = None
        if leg_a.price is not None and leg_b.price is not None:
            entry_spread = leg_b.price - float(pair.hedge_ratio) * leg_a.price
        # In SPREADS, from what leg B actually holds — the same unit the
        # trader clicked in, so a partial fill reads as 0.5 spreads
        # rather than as a lot count nobody sized in.
        quantity = (leg_b.volume / pair.clip_lots_b
                    if pair.clip_lots_b else leg_b.volume)
        position = SpreadPosition(
            pair.key, group.side, quantity, leg_a, leg_b, entry_spread,
            OrderType.LIMIT, sizing.spread_units(leg_b.volume, contract_b),
            clock=self.executor.clock)
        position.click_to_on_ms = elapsed_ms
        # Scored against the level the trader NAMED: a maker fill's
        # benchmark is the clicked level, not the touch it crossed.
        position.entry_slippage = slippage(group.level, entry_spread,
                                           group.side)
        return self.book.add_position(position)

    def _settle_orders(self, group, filled_spreads):
        """Mark the synthetics behind a fill, OLDEST first.

        A partial fill fills the earliest clicks and leaves the rest
        working — queue order, which is the only fair reading of "three
        separate orders at one price".
        """
        remaining = filled_spreads
        for order in sorted((o for o in group.orders if o.is_working),
                            key=lambda o: o.created_at):
            if remaining <= 1e-9:
                break
            take = min(order.remaining, remaining)
            order.filled_quantity += take
            remaining -= take
            if order.remaining <= 1e-9:
                order.state = OrderState.FILLED

    # -- what the monitor renders -------------------------------------------

    def snapshot(self, pair_key=None):
        """Every group, with the leg its real pending is ON named.

        In LIMIT mode the pending rests on ONE leg and the other is
        crossed at market when it fills. Which leg that is, and at what
        price, is the difference between "my order is at the broker"
        and "half of it is" — and it was only ever a bare 'a'/'b' on
        one panel, so leg A looked like it had no order at all.
        """
        out = []
        for group in self.groups.values():
            if pair_key is not None and group.pair_key != pair_key:
                continue
            row = group.to_dict()
            pair = self.config.pairs.get(group.pair_key)
            if pair is not None:
                on_a = group.leg == 'a'
                row['account'] = pair.account_a if on_a else pair.account_b
                row['symbol'] = pair.symbol_a if on_a else pair.symbol_b
                row['crosses'] = pair.symbol_b if on_a else pair.symbol_a
                row['crosses_leg'] = 'B' if on_a else 'A'
            out.append(row)
        return out
