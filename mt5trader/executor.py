"""Getting a pair ON, and getting it OFF again.

This module owns the two dangerous moments in the product: the naked
window between one leg being on and the other landing, and the close
that must target a position TICKET because these accounts are hedging
mode.

MARKET mode only lives here for now (spec §14: get flat-and-back-to-flat
exactly right before anything rests). The LIMIT path reuses everything
below it — the hedge arithmetic, the deadline, the unwind, the
ticket-based closes — and differs only in how the first leg gets on.

Three rules, each of which cost money in the system this is ported
from:

1. **The crossing order goes IMMEDIATELY on fill — not after a wait.**
   `LEG_DEADLINE_SEC` is a failure-ESCALATION window, not a patience
   window. A market order round-trips in ~24ms; 2.0s is ~80x headroom
   and only fires on a real fault.
2. **Only a REJECTED crossing leg unwinds.** Slow is not rejected.
3. **The unwind CLOSES, it does not offset.** A plain opposite market
   order on a hedging account opens a SECOND position; that has already
   happened live, and the reconciler found both 60 seconds later.
"""

import logging
import time

from . import costs, sizing
from .models import (LegFill, OrderSide, OrderType, SpreadPosition,
                     SpreadSide, new_id)
from .spread import closing_prices, executable_spread


class ExecutionResult:
    """What a click did, in the words the ladder will print.

    `reason` is never "check the log" — it carries the broker's own
    words where the broker is the one who refused (spec §11).
    """

    def __init__(self, ok, position=None, reason=None, refused=False,
                 naked=None, elapsed_ms=None, legs=None):
        self.ok = ok
        self.position = position
        self.reason = reason
        #: Refused BEFORE anything moved — no money at risk. Distinct
        #: from a failure that left a leg on.
        self.refused = refused
        #: Set when a leg is on and its hedge is not. The loudest thing
        #: on the screen (spec §16).
        self.naked = naked
        self.elapsed_ms = elapsed_ms
        self.legs = legs or {}

    def to_dict(self):
        return {'ok': self.ok, 'reason': self.reason, 'refused': self.refused,
                'naked': self.naked, 'elapsed_ms': self.elapsed_ms,
                'position': self.position.to_dict() if self.position else None,
                'legs': {k: (v.to_dict() if hasattr(v, 'to_dict') else v)
                         for k, v in self.legs.items()}}


class PairExecutor:
    """Executes one click on one pair, across two accounts."""

    def __init__(self, config, legs, clock=time.time, sleep=time.sleep):
        self.config = config
        #: {account name: LocalLeg | RemoteLeg}
        self.legs = legs
        self.clock = clock
        self.sleep = sleep
        #: Every fill's click->pair-on time, so the operator can see
        #: whether 2.0s is the right deadline (spec §4, §16).
        self.timings = []

    # -- entry ----------------------------------------------------------

    def market_entry(self, pair, side, md, spreads=None, clicked_level=None):
        """Cross both legs now. The clicked price is a slippage guard.

        Returns an ExecutionResult. Nothing here loops or re-enters: one
        click is one order (spec, decision 5).
        """
        side = SpreadSide(getattr(side, 'value', side))
        spreads = float(spreads if spreads is not None
                        else pair.default_quantity)
        started = self.clock()

        refusal = self.precheck(pair, side, md, spreads, clicked_level)
        if refusal is not None:
            return ExecutionResult(False, reason=refusal, refused=True)

        plan = self.size(pair, md, spreads)
        leg_a_side, leg_b_side = side.leg_sides()
        # The decision touch — what the market offered at the moment the
        # order was decided. Slippage is scored against THIS, not the
        # mid (spec §5).
        decision_spread = executable_spread(md, side)

        first, second = self.crossing_order(pair)
        sides = {'a': leg_a_side, 'b': leg_b_side}
        volumes = {'a': plan['leg_a_lots'], 'b': plan['leg_b_lots']}
        contracts = {'a': plan['leg_a_contract'], 'b': plan['leg_b_contract']}
        tag = new_id('LADDER')

        # The harder-to-fill leg FIRST. Filling the easy leg and then
        # discovering the hard one will not fill is how you end up naked.
        first_fill = self._send_leg(pair, first, sides[first], volumes[first],
                                    contracts[first], tag)
        if not first_fill.get('ok'):
            # Nothing is on. This is a refusal, not a naked position.
            return ExecutionResult(
                False, refused=True,
                reason=(f"leg {first.upper()} refused: "
                        f"{first_fill.get('error')}"))

        # IMMEDIATELY — no wait, no poll interval, no patience.
        second_fill = self._cross_with_deadline(
            pair, second, sides[second], volumes[second], contracts[second],
            tag)

        elapsed_ms = (self.clock() - started) * 1000.0
        fills = {first: first_fill, second: second_fill}

        if not second_fill.get('ok'):
            # A genuine rejection. Unwind the leg that IS on, by ticket.
            reason = (f"leg {second.upper()} rejected: "
                      f"{second_fill.get('error')} — unwinding leg "
                      f"{first.upper()}")
            logging.critical("%s: %s", pair.key, reason)
            unwind = self._unwind_leg(pair, first, sides[first], first_fill)
            naked = None if unwind.get('ok') else {
                'leg': first.upper(),
                'symbol': self._symbol(pair, first),
                'volume': first_fill.get('filled_volume'),
                'tickets': first_fill.get('position_tickets'),
                'why': unwind.get('error'),
            }
            return ExecutionResult(False, reason=reason, naked=naked,
                                   elapsed_ms=elapsed_ms, legs=fills)

        matched = self._matched_fraction(plan, fills)
        if matched < float(self.config.get('MIN_MATCHED_FRACTION', 0.4)):
            reason = (f"only {matched:.0%} of the clip matched on both legs "
                      f"— unwinding rather than holding a part-hedged pair")
            logging.warning("%s: %s", pair.key, reason)
            self._unwind_leg(pair, 'a', sides['a'], fills['a'])
            self._unwind_leg(pair, 'b', sides['b'], fills['b'])
            return ExecutionResult(False, reason=reason,
                                   elapsed_ms=elapsed_ms, legs=fills)

        position = self._book_position(pair, side, spreads, plan, fills,
                                       sides, decision_spread, elapsed_ms)
        self.timings.append(elapsed_ms)
        return ExecutionResult(True, position=position,
                               elapsed_ms=elapsed_ms, legs=fills)

    # -- the checks that happen BEFORE anything moves ---------------------

    def precheck(self, pair, side, md, spreads, clicked_level=None):
        """A one-line reason to refuse this click, or None.

        Both legs are verified up front: a pair whose child order is
        under either leg's minimum must be refused before any money
        moves — and, on the quoting path, before anything rests at the
        broker.
        """
        if md is None:
            return (f"{pair.key} has no price yet — both legs need a live "
                    f"quote before an order can be sized")
        for leg in ('a', 'b'):
            account = pair.account_a if leg == 'a' else pair.account_b
            if account not in self.legs:
                return (f"account '{account}' (leg {leg.upper()}) is not "
                        f"connected — start its leg runner")

        plan = self.size(pair, md, spreads)
        if plan.get('reason'):
            return plan['reason']

        guard = md.get('guard_reason')
        if guard:
            # A click is the trader looking at the screen, so the ladder
            # shows the guard's state continuously (spec §8) — but a
            # market order into a stale or desynced print is still
            # refused, with the reason, not silently swallowed.
            return f"{guard} — the click was not sent"

        if clicked_level is not None:
            breach = self.protection_breach(pair, side, md, clicked_level)
            if breach:
                return breach
        return None

    def protection_breach(self, pair, side, md, clicked_level):
        """Market-with-protection: refuse a fill worse than the clicked
        spread by more than MARKET_PROTECTION_TICKS.

        Without it, a click anywhere on the ladder fills at whatever the
        touch happens to be, which on a desynced print is exactly the
        $20.40 fault the jump guard exists for.
        """
        increment = pair.effective_increment()
        if not increment:
            return None                      # nothing to measure in
        ticks = float(self.config.get('MARKET_PROTECTION_TICKS', 3.0) or 0.0)
        if ticks <= 0:
            return None
        touch = executable_spread(md, side)
        if touch is None:
            return None
        # Worse means: paying MORE than clicked to buy, receiving LESS
        # than clicked to sell.
        slip = (touch - clicked_level) if side is SpreadSide.BUY \
            else (clicked_level - touch)
        allowed = ticks * increment
        if slip > allowed:
            return (f"the market is {slip:.4g} through your {clicked_level:.4g} "
                    f"— more than the {ticks:g} x {increment:g} protection. "
                    f"Nothing was sent; click again at the price on screen.")
        return None

    def size(self, pair, md, spreads):
        """Leg lots and `k` for this click — one place, one answer."""
        return sizing.clip_plan(pair, pair.meta_a or {}, pair.meta_b or {},
                                (md or {}).get('leg_a_mid'),
                                (md or {}).get('leg_b_mid'), spreads)

    def crossing_order(self, pair):
        """(first, second) leg keys — the harder-to-fill leg FIRST.

        Harder means: the wider book, the larger minimum volume, or the
        slower feed — usually the future. Measured from the legs' own
        MT5 metadata rather than assumed, and the ladder shows both
        widths so the operator can see why.
        """
        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        score_a = (meta_a.get('volume_min') or 0.0, meta_a.get('width') or 0.0)
        score_b = (meta_b.get('volume_min') or 0.0, meta_b.get('width') or 0.0)
        return ('b', 'a') if score_b >= score_a else ('a', 'b')

    # -- leg mechanics ---------------------------------------------------

    def _symbol(self, pair, leg):
        return pair.symbol_a if leg == 'a' else pair.symbol_b

    def _leg(self, pair, leg):
        account = pair.account_a if leg == 'a' else pair.account_b
        return self.legs.get(account)

    def _send_leg(self, pair, leg, side, volume, contract, tag):
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no leg runner for {symbol}'}
        result = runner.order(
            symbol, side.value, volume,
            slippage_points=float(self.config.get('SLIPPAGE_POINTS', 1.0)),
            # Stamped so our own orders are tellable from the trader's
            # terminal clicks in the order log (spec §6).
            comment=f'{tag}:{leg.upper()}')
        result = dict(result or {})
        result['symbol'] = symbol
        result['side'] = side.value
        result['contract_size'] = contract
        return result

    def _cross_with_deadline(self, pair, leg, side, volume, contract, tag):
        """Send the crossing leg, retrying only until the deadline.

        The deadline is not patience — there is nothing to be patient
        for, the hedge must go on. It bounds how long a genuine fault
        (a broker not answering, a filling mode nothing accepts) can
        leave us naked before it is escalated.
        """
        deadline = self.clock() + float(
            self.config.get('LEG_DEADLINE_SEC', 2.0))
        attempt = 0
        result = None
        while True:
            attempt += 1
            result = self._send_leg(pair, leg, side, volume, contract, tag)
            if result.get('ok'):
                result['attempts'] = attempt
                return result
            if self.clock() >= deadline:
                result['attempts'] = attempt
                result['error'] = (f"{result.get('error')} (after {attempt} "
                                   f"attempts in "
                                   f"{self.config.get('LEG_DEADLINE_SEC')}s)")
                return result
            self.sleep(0.02)

    def _matched_fraction(self, plan, fills):
        """How much of the intended clip is hedged on BOTH legs.

        Hedge to what actually FILLED, not to what was sent: a pending
        can fill partially, and a market order under IOC can too.
        """
        wanted_a = plan['leg_a_lots'] or 0.0
        wanted_b = plan['leg_b_lots'] or 0.0
        got_a = (fills.get('a') or {}).get('filled_volume') or 0.0
        got_b = (fills.get('b') or {}).get('filled_volume') or 0.0
        if not wanted_a or not wanted_b:
            return 0.0
        return min(got_a / wanted_a, got_b / wanted_b)

    def _unwind_leg(self, pair, leg, entry_side, fill):
        """Close what is on, BY TICKET, newest first.

        Never an opposite market order: on a hedging account that opens
        a second position and leaves the first one live.
        """
        runner = self._leg(pair, leg)
        symbol = self._symbol(pair, leg)
        if runner is None:
            return {'ok': False, 'error': f'no leg runner for {symbol}'}
        tickets = list(fill.get('position_tickets') or [])
        if not tickets:
            return {'ok': False,
                    'error': f'leg {leg.upper()} filled but reported no '
                             f'position ticket — close {symbol} by hand'}
        return self.close_tickets(runner, symbol, tickets, entry_side,
                                  comment='UNWIND')

    def close_tickets(self, runner, symbol, tickets, entry_side,
                      volume=None, comment='CXL'):
        """Close specific position tickets, taking volumes from the
        BROKER's book rather than from what we sent.

        A ticket may be partly closed already, and asking to close more
        than is there fails the whole request. A ticket MT5 no longer
        lists is already gone — that is not an error.
        """
        entry_side = OrderSide(getattr(entry_side, 'value', entry_side))
        live = runner.positions(symbol)
        if live is None:
            # None means the leg could not be read — NOT that it is flat.
            return {'ok': False, 'error': f'{symbol}: the broker could not '
                                          f'be read; nothing was closed'}
        by_ticket = {str(p['ticket']): p for p in live}
        remaining = None if volume is None else float(volume)
        closed, errors = [], []
        # Newest first, so a partial unwind takes off the piece that was
        # most recently added.
        for ticket in sorted((str(t) for t in tickets), reverse=True):
            position = by_ticket.get(ticket)
            if position is None:
                continue                    # already gone, not an error
            want = float(position['volume'])
            if remaining is not None:
                want = min(want, remaining)
                if want <= 0:
                    break
            result = runner.close_ticket(symbol, int(ticket), want,
                                         entry_side.value, comment=comment)
            if result.get('ok'):
                closed.append({'ticket': ticket, 'volume': want,
                               'price': result.get('price')})
                if remaining is not None:
                    remaining -= want
            else:
                errors.append(f"{ticket}: {result.get('error')}")
        if errors:
            return {'ok': False, 'closed': closed,
                    'error': '; '.join(errors)}
        return {'ok': True, 'closed': closed}

    # -- exit -------------------------------------------------------------

    def close_position(self, pair, position, md=None, reason='manual'):
        """Flatten one spread position, both legs, by ticket.

        A guard NEVER prevents a close (spec §8): a trade must always be
        closable, so nothing in here consults the staleness or jump
        state.
        """
        leg_a_side, leg_b_side = position.side.leg_sides()
        results = {}
        for leg, entry_side, fill in (('a', leg_a_side, position.leg_a),
                                      ('b', leg_b_side, position.leg_b)):
            runner = self._leg(pair, leg)
            symbol = self._symbol(pair, leg)
            if runner is None:
                results[leg] = {'ok': False,
                                'error': f'no leg runner for {symbol}'}
                continue
            results[leg] = self.close_tickets(
                runner, symbol, (fill.position_tickets if fill else []),
                entry_side, comment='CLOSE')

        ok = all(r.get('ok') for r in results.values())
        if ok:
            position.closed_at = self.clock()
            position.close_reason = reason
            exit_spread = executable_spread(md, position.side, closing=True) \
                if md else None
            position.exit_spread = exit_spread
            gross = position.mark(exit_spread)
            fees = costs.mark_fees(
                position.leg_a.volume if position.leg_a else 0.0,
                position.leg_b.volume if position.leg_b else 0.0,
                self.config.settings)
            position.realized_pnl = (None if gross is None else gross - fees)
        else:
            # A close that did not go through leaves the position OPEN
            # and ACTIVE. Marking it ERROR/CLOSING would remove it from
            # every active lookup — the ladder, the monitor and the
            # reconciler's known-ticket set — while the money sits at
            # the broker (spec §7).
            logging.error("%s: close failed, position stays ACTIVE: %s",
                          pair.key, results)
        return {'ok': ok, 'legs': results,
                'position': position.to_dict()}

    # -- bookkeeping ------------------------------------------------------

    def _book_position(self, pair, side, spreads, plan, fills, sides,
                       decision_spread, elapsed_ms):
        fill_a, fill_b = fills['a'], fills['b']
        leg_a = LegFill(pair.account_a, pair.symbol_a, sides['a'],
                        fill_a.get('filled_volume') or 0.0,
                        fill_a.get('price'),
                        order_ticket=fill_a.get('ticket'),
                        position_tickets=fill_a.get('position_tickets'),
                        contract_size=plan['leg_a_contract'], clock=self.clock)
        leg_b = LegFill(pair.account_b, pair.symbol_b, sides['b'],
                        fill_b.get('filled_volume') or 0.0,
                        fill_b.get('price'),
                        order_ticket=fill_b.get('ticket'),
                        position_tickets=fill_b.get('position_tickets'),
                        contract_size=plan['leg_b_contract'], clock=self.clock)

        # Anchored on the EXECUTED fills, never on the mid the decision
        # was taken at. Four rows agreeing to the cent proves they share
        # an anchor, not that the anchor is right (spec §11).
        entry_spread = None
        if leg_a.price is not None and leg_b.price is not None:
            entry_spread = leg_b.price - float(pair.hedge_ratio) * leg_a.price

        position = SpreadPosition(
            pair.key, side, spreads, leg_a, leg_b, entry_spread,
            OrderType.MARKET,
            sizing.spread_units(leg_b.volume, plan['leg_b_contract']),
            clock=self.clock)
        position.click_to_on_ms = elapsed_ms
        position.entry_slippage = slippage(decision_spread, entry_spread, side)
        return position


def slippage(expected, filled, side, closing=False):
    """Positive is a COST, always — including on exits.

    A short buys the spread back to close, so the sign flips between
    entry and exit; getting that backwards reports every exit's cost as
    a gain. Unmeasured returns None, which the UI renders as "—" (spec
    §5, §11).
    """
    if expected is None or filled is None:
        return None
    side = SpreadSide(getattr(side, 'value', side))
    buying = (side is SpreadSide.BUY) != bool(closing)
    return (filled - expected) if buying else (expected - filled)


def mark_position(position, md, settings):
    """(gross, net, closing_spread) for an open position, marked at the
    touches it would actually CLOSE at.

    Consequence, and it is correct: a position shows a loss the instant
    it opens, equal to one round turn of both legs' bid-ask. That is
    what closing immediately would cost, and the UI says so rather than
    hiding it.

    Only COMMISSION is subtracted. The crossing is already in the two
    prices; subtracting the round trip again is the bid-ask twice.
    """
    closing_spread = executable_spread(md, position.side, closing=True)
    gross = position.mark(closing_spread)
    if gross is None:
        return None, None, closing_spread
    fees = costs.mark_fees(
        position.leg_a.volume if position.leg_a else 0.0,
        position.leg_b.volume if position.leg_b else 0.0, settings)
    return gross, gross - fees, closing_spread


def leg_marks(position, md):
    """The two prices this position would be closed at, per leg.

    Agrees with `mark_position` by construction: `B - beta x A` of these
    two IS the closing executable spread, and a test pins that.
    """
    return closing_prices(md, position.side)
