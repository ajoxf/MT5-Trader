"""The coordinator: two feeds in, one spread per pair out.

It is the only process that sees both accounts at once. Each poll it
reads a tick from each leg runner, fuses them into a spread per pair,
runs the guards, and publishes one status snapshot that the ladders,
the Market Grid and the positions monitor all render from — so a number
cannot disagree with itself between panels, and the grid costs no extra
MT5 round trips.

What it must not do: poll `account_info` on every pass (it is an IPC
round trip, cached ~5s), and lag the panel beside it. The achieved loop
interval is published so "is the engine or the browser slow?" is
answerable rather than guessed at.
"""

import json
import logging
import os
import threading
import time

from . import hedgeratio, sizing
from .book import Book
from .executor import PairExecutor, mark_position
from .models import MAGIC_NUMBER, OrderType
from .quoter import Quoter
from .reconcile import Reconciler
from .session import SessionClock, day_orders, overnight_action
from .spread import (LevelSigma, QuoteAgeTracker, SpreadJumpTracker,
                     compute_spread, stale_quote)


class Coordinator:
    def __init__(self, config, legs, status_path='status.json',
                 clock=time.time, sleep=time.sleep,
                 monotonic=time.monotonic):
        self.config = config
        self.legs = legs                    # {account name: leg}
        self.status_path = status_path
        self.clock = clock
        self.sleep = sleep
        self.book = Book()
        self.executor = PairExecutor(config, legs, clock=clock, sleep=sleep)
        self.quoter = Quoter(config, legs, self.executor, self.book)
        self.reconciler = Reconciler(config, legs, self.book, self.executor,
                                     clock=clock)
        self._last_reconcile = None
        self.session_clock = SessionClock(config)
        #: What the cutoff did, per day, for the monitor to show.
        self.session_events = []
        # The guards measure on a MONOTONIC clock and the quote's own
        # identity, never on the broker's timestamp — a clock offset
        # would poison a guard that gates real orders.
        self.quote_ages = QuoteAgeTracker(monotonic)
        self.jumps = SpreadJumpTracker(monotonic)
        self.sigmas = {}                    # pair key -> LevelSigma
        self.market = {}                    # pair key -> snapshot
        self.session = {}                   # pair key -> O/H/L/V of OUR series
        self._account_cache = {}            # account -> (at, info)
        self._loop_interval = None
        self._last_poll = None
        self._stop = threading.Event()
        #: Symbol resolution failures, in the operator's words, kept on
        #: the snapshot so a broken pair is VISIBLE rather than absent
        #: (spec §17: never hide a broken row).
        self.errors = {}

    # -- startup ------------------------------------------------------------

    def start(self):
        """Sweep first, then resolve symbols, then poll.

        The sweep is before ANYTHING else: a pending of ours still
        resting is from a previous life, and one that filled while we
        were down is an unhedged outright position nobody was watching.
        """
        swept = self.sweep_pendings('startup')
        self.resolve_symbols()
        return swept

    def sweep_pendings(self, when):
        """Cancel every pending of OURS on both accounts, and verify.

        Magic-scoped, so the trader's own terminal orders are never
        touched. A pending we failed to cancel is a CRITICAL line, not a
        warning — it can fill unhedged with nobody watching.
        """
        report = {'when': when, 'cancelled': [], 'failed': [], 'unknown': []}
        for name, leg in self.legs.items():
            pendings = leg.pending_orders()
            if pendings is None:
                # None means the leg could not be read — NOT "no orders".
                report['unknown'].append(name)
                logging.critical(
                    "%s sweep: account '%s' could not be read, so it is "
                    "UNKNOWN whether pendings of ours are resting there",
                    when, name)
                continue
            for pending in pendings:
                result = leg.cancel_order(pending['ticket'])
                entry = dict(pending, account=name)
                if result.get('cancelled'):
                    if result.get('filled_volume'):
                        # It filled before the cancel landed. That is an
                        # orphan position, not a tidy cancel.
                        entry['leaked_fill'] = result.get('filled_volume')
                        entry['position_tickets'] = result.get(
                            'position_tickets')
                        logging.critical(
                            "%s sweep: pending %s on '%s' FILLED before it "
                            "could be cancelled — %s lots are on with no "
                            "hedge", when, pending['ticket'], name,
                            result.get('filled_volume'))
                    report['cancelled'].append(entry)
                else:
                    entry['error'] = result.get('error')
                    report['failed'].append(entry)
                    logging.critical(
                        "%s sweep: could NOT cancel pending %s on '%s': %s",
                        when, pending['ticket'], name, result.get('error'))
        return report

    def resolve_symbols(self):
        """Read both legs' contract specs from MT5 — never typed in.

        A failure names what the account actually offers, because
        brokers spell gold XAUUSD, GOLD, XAUUSD.r, and an account that
        offers none of them is probably the wrong leg.
        """
        for key, pair in self.config.pairs.items():
            problems = []
            for leg_key, meta_attr in (('a', 'meta_a'), ('b', 'meta_b')):
                account = pair.account_a if leg_key == 'a' else pair.account_b
                symbol = pair.symbol_a if leg_key == 'a' else pair.symbol_b
                leg = self.legs.get(account)
                if leg is None:
                    problems.append(
                        f"leg {leg_key.upper()}: account '{account}' has no "
                        f"leg runner — start it, or fix the account on this "
                        f"pair")
                    continue
                report = leg.symbol_report(symbol)
                if not report.get('found'):
                    offered = _offered(leg, symbol)
                    names = ', '.join(s['symbol'] for s in offered[:8])
                    problems.append(
                        f"leg {leg_key.upper()}: '{symbol}' is not on account "
                        f"'{account}'. " + (
                            f"It offers {names}." if names else
                            f"Nothing there resembles it — this is probably "
                            f"the wrong account for this leg."))
                    continue
                setattr(pair, meta_attr, _meta_from_report(report))
            self.errors[key] = problems
            if not problems:
                self._settle_beta(pair)
                self._settle_clip(pair)
        return self.errors

    def _settle_beta(self, pair):
        """Re-derive beta when it was stamped for a DIFFERENT pair.

        An operator who tuned beta on their own pair keeps their number;
        a beta left behind by an instrument change does not get to
        silently define the spread.
        """
        price_a = (pair.meta_a or {}).get('mid')
        price_b = (pair.meta_b or {}).get('mid')
        beta, why = hedgeratio.resolve(
            pair.hedge_ratio, pair.hedge_ratio_for, pair.pair_type,
            pair.symbol_a, pair.symbol_b, price_a, price_b)
        if beta is not None:
            logging.warning("%s: hedge ratio %g -> %g (%s)",
                            pair.key, pair.hedge_ratio, beta, why)
            pair.hedge_ratio = beta
        pair.hedge_ratio_for = hedgeratio.pair_signature(pair.symbol_a,
                                                         pair.symbol_b)
        pair.beta_reason = why

    def _settle_clip(self, pair):
        """What ONE spread means in leg lots, when it was not configured.

        The default is the smallest size at which BOTH legs clear their
        own minimum volume — quoting the click in spreads makes a size
        that implies a sub-minimum hedge unrepresentable.
        """
        if pair.clip_lots_a and pair.clip_lots_b:
            return
        meta_a, meta_b = pair.meta_a or {}, pair.meta_b or {}
        pair.clip_lots_a, pair.clip_lots_b = sizing.matched_minimum_lots(
            meta_a.get('volume_min'), meta_b.get('volume_min'),
            meta_a.get('volume_step'), meta_b.get('volume_step'),
            pair.hedge_ratio, meta_a.get('contract_size'),
            meta_b.get('contract_size'))

    # -- the loop ------------------------------------------------------------

    def poll_once(self):
        """One pass over every enabled pair. Returns the snapshot dict."""
        started = self.clock()
        if self._last_poll is not None:
            self._loop_interval = started - self._last_poll
        self._last_poll = started

        ticks = self._read_ticks()
        for key, pair in self.config.enabled_pairs().items():
            tick_a = ticks.get((pair.account_a, pair.symbol_a))
            tick_b = ticks.get((pair.account_b, pair.symbol_b))
            if not tick_a or not tick_b:
                self.market[key] = None
                continue
            md = compute_spread(pair, tick_a, tick_b, pair.hedge_ratio,
                                clock=self.clock)
            self.quote_ages.observe(key, md)
            sigma = self.sigmas.setdefault(
                key, LevelSigma(self.config.get('SIGMA_WINDOW_QUOTES', 600)))
            sigma.observe(md)
            md['spread_sigma'] = sigma.sigma

            stale = stale_quote(md, self.config.get('MAX_QUOTE_AGE_SEC'))
            jumped = self.jumps.observe(
                key, md, sigma.sigma,
                self.config.get('MAX_SPREAD_JUMP_SIGMA'),
                self.config.get('JUMP_SETTLE_SEC'))
            md['stale_reason'] = stale
            md['jump_reason'] = jumped
            md['guard_reason'] = stale or jumped
            # The badge the ladder shows continuously, so a trader can
            # see what they are clicking into (spec §8).
            md['feed_badge'] = _badge(md, stale, jumped)
            self._observe_session(key, md)
            self.market[key] = md
            # LIMIT-mode orders are worked on the SAME pass that priced
            # them: a peg re-priced off a snapshot older than the one on
            # screen is a peg holding a level nobody is showing.
            self.quoter.work(pair, md)
        return self.market

    def _read_ticks(self):
        """One tick per (account, symbol), fetched once per poll.

        Two pairs on the same symbol cost one round trip, not two: the
        poll budget is two IPC calls per pair per pass and it is the
        thing that decides whether the ladder keeps up.
        """
        wanted = set()
        for pair in self.config.enabled_pairs().values():
            wanted.add((pair.account_a, pair.symbol_a))
            wanted.add((pair.account_b, pair.symbol_b))
        ticks = {}
        for account, symbol in wanted:
            leg = self.legs.get(account)
            if leg is None or not symbol:
                continue
            tick = leg.tick(symbol)
            if tick:
                ticks[(account, symbol)] = tick
        return ticks

    def _observe_session(self, key, md):
        """O/H/L/V of OUR OWN series, since this process started.

        MT5 has no session statistics for a spread that does not exist,
        so these are ours and the UI labels them as ours. A borrowed
        number here would be read as the exchange's.
        """
        spread = md.get('spread')
        session = self.session.get(key)
        if session is None:
            session = {'open': spread, 'high': spread, 'low': spread,
                       'volume': 0.0, 'ours': True}
            self.session[key] = session
        session['high'] = max(session['high'], spread)
        session['low'] = min(session['low'], spread)
        session['volume'] = sum(p['quantity']
                                for p in self.book.prints.get(key, ()))
        md['session'] = dict(session)
        md['net_change'] = (spread - session['open']
                            if session['open'] is not None else None)

    # -- what a click does ---------------------------------------------------

    def click(self, pair_key, side, level, quantity=None):
        """One click on one row. One click is ONE order.

        MARKET crosses both legs now with the clicked price as the
        slippage guard; LIMIT creates a synthetic working order at that
        level, backed by a real pending on the quoting leg. Three clicks
        at one price are three orders, individually cancellable, however
        they are aggregated at the broker.

        The Market Grid and the ladder both come through here, so a grid
        click and a ladder click at the same price are the same order.
        """
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        md = self.market.get(pair_key)
        quantity = pair.default_quantity if quantity is None else quantity

        if pair.order_type is OrderType.MARKET:
            result = self.executor.market_entry(pair, side, md, quantity,
                                                level)
            if result.ok and result.position:
                self.book.add_position(result.position)
            return result.to_dict()

        refusal = self.executor.precheck(pair, side, md, quantity)
        if refusal is not None and md is None:
            # No price at all is a refusal either way. A guard reason is
            # NOT: a resting order simply waits for a quote it can rest
            # on (spec §8), which is what the quoter does.
            return {'ok': False, 'refused': True, 'reason': refusal}
        order = self.book.add_order(pair, side, level, quantity)
        self.quoter.group_for(pair, order)
        return {'ok': True, 'order': order.to_dict()}

    def cancel_order(self, order_id):
        order = self.book.order(order_id)
        if order is None or not order.is_working:
            return {'ok': False, 'reason': 'that order is not working'}
        self.quoter.cancel(order)
        return {'ok': True, 'order': order.to_dict()}

    def cancel_where(self, pair_key=None, side=None, reason='cancelled'):
        """`CXL B` / `CXL S` / `CXL All`, and the global kill.

        The real pendings behind them are re-sized or pulled on the next
        pass, which is also where a fill that raced the cancel is
        caught.
        """
        pulled = self.book.cancel_where(pair_key, side, reason)
        for key, pair in self.config.pairs.items():
            if pair_key in (None, key):
                self.quoter.work(pair, self.market.get(key))
        return pulled

    def account_info(self, name):
        """Cached ~5s. Fetching this three times a second is three IPC
        round trips a second for a number that moves slowly."""
        ttl = float(self.config.get('ACCOUNT_INFO_CACHE_SEC', 5.0))
        cached = self._account_cache.get(name)
        now = self.clock()
        if cached and now - cached[0] < ttl:
            return cached[1]
        leg = self.legs.get(name)
        info = leg.account_info() if leg else None
        self._account_cache[name] = (now, info)
        return info

    # -- what the UI renders --------------------------------------------------

    def snapshot(self):
        """Everything every panel needs, from one poll's data."""
        pairs = {}
        for key, pair in self.config.pairs.items():
            md = self.market.get(key)
            net, avg_entry = self.book.net_position(key)
            buys, sells = self.book.working_counts(key)
            positions = []
            open_pnl = 0.0
            for position in self.book.positions(key):
                gross, net_pnl, closing = mark_position(
                    position, md, self.config.settings)
                row = position.to_dict()
                row.update({'gross_pnl': gross, 'net_pnl': net_pnl,
                            'closing_spread': closing})
                positions.append(row)
                if net_pnl is not None:
                    open_pnl += net_pnl
            pairs[key] = {
                'key': key, 'name': pair.name, 'enabled': pair.enabled,
                'account_a': pair.account_a, 'account_b': pair.account_b,
                'symbol_a': pair.symbol_a, 'symbol_b': pair.symbol_b,
                'hedge_ratio': pair.hedge_ratio,
                'hedge_ratio_for': pair.hedge_ratio_for,
                'increment': pair.effective_increment(),
                'increment_derived': pair.derived_increment(),
                'order_type': pair.order_type.value,
                'time_in_force': pair.time_in_force.value,
                'overnight': pair.overnight.value,
                'default_quantity': pair.default_quantity,
                'clip_lots_a': pair.clip_lots_a,
                'clip_lots_b': pair.clip_lots_b,
                'spread_units': sizing.spread_units(
                    pair.clip_lots_b, (pair.meta_b or {}).get('contract_size')),
                'market': md,
                'short_spread': (md or {}).get('short_spread'),
                'long_spread': (md or {}).get('long_spread'),
                'errors': self.errors.get(key) or [],
                'orders': [o.to_dict() for o in self.book.orders(key)],
                'quotes': self.quoter.snapshot(key),
                'quoting_leg': pair.quoting_leg,
                'leg_a_width': (pair.meta_a or {}).get('width'),
                'leg_b_width': (pair.meta_b or {}).get('width'),
                'working_buys': buys, 'working_sells': sells,
                'positions': positions,
                'net_position': net, 'avg_entry': avg_entry,
                'open_pnl': open_pnl if positions else None,
                'last_print': self.book.last_print(key),
            }
        return {
            'at': self.clock(),
            # Published so a slow ladder can be blamed on the right
            # process instead of argued about (spec §9).
            'loop_interval_sec': self._loop_interval,
            'poll_target_sec': self.config.get('POLL_INTERVAL_SEC'),
            'accounts': {name: self.account_info(name) for name in self.legs},
            'reconciler': self.reconciler.snapshot(),
            'session_events': self.session_events[-50:],
            'hedge_times_ms': list(self.quoter.hedge_times[-50:]),
            'click_to_on_ms': list(self.executor.timings[-50:]),
            'pairs': pairs,
            'magic': MAGIC_NUMBER,
        }

    def publish(self):
        """Write the snapshot through a tmp file and `os.replace`.

        The web process reads this file while we write it; a plain
        `open(path, 'w')` means it eventually reads half a JSON
        document.
        """
        tmp = self.status_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.snapshot(), f, default=str)
        os.replace(tmp, self.status_path)

    def run(self):
        interval = float(self.config.get('POLL_INTERVAL_SEC', 0.3))
        self.start()
        while not self._stop.is_set():
            began = self.clock()
            try:
                self.poll_once()
                self.publish()
            except Exception as e:                     # never die on one poll
                logging.exception("poll failed: %s", e)
            self.run_session_cutoff()
            self.reconcile_if_due()
            elapsed = self.clock() - began
            self.sleep(max(0.0, interval - elapsed))

    def run_session_cutoff(self):
        """At the cutoff: cancel DAY orders, then apply each ladder's
        overnight rule. Once a day, per pair.

        Working orders and positions are governed by DIFFERENT settings
        (spec §3.1 and §3.2) and this is the one place both are read, so
        a trader configures a single time.
        """
        events = []
        for key, pair in self.config.pairs.items():
            if not self.session_clock.due(key):
                continue
            self.session_clock.mark(key)
            expired = day_orders(self.book.orders(key))
            for order in expired:
                self.cancel_order(order.order_id)
            if expired:
                events.append({'pair': key, 'action': 'day_orders_cancelled',
                               'count': len(expired)})
            md = self.market.get(key)
            for position in self.book.positions(key):
                _gross, net_pnl, _closing = mark_position(
                    position, md, self.config.settings)
                verdict = overnight_action(
                    pair.overnight, net_pnl, self.session_clock.now(),
                    self.config.get('OVERNIGHT_CLOSE_HOUR'),
                    self.config.get('OVERNIGHT_CLOSE_MINUTE'))
                if verdict is None:
                    continue
                # Urgent: market, by ticket, never resting. And no guard
                # withholds it.
                result = self.executor.close_position(pair, position, md,
                                                      reason=verdict)
                events.append({'pair': key, 'action': 'overnight_close',
                               'position': position.position_id,
                               'mode': pair.overnight.value,
                               'net_pnl': net_pnl, 'ok': result['ok']})
        self.session_events.extend(events)
        return events

    def reconcile_if_due(self):
        """Every ~20s in LIVE. Assume you will lose track."""
        every = float(self.config.get('RECONCILE_INTERVAL_SEC', 20.0) or 0.0)
        if every <= 0:
            return None
        now = self.clock()
        if self._last_reconcile is not None and now - self._last_reconcile < every:
            return None
        self._last_reconcile = now
        try:
            return self.reconciler.run()
        except Exception as e:                      # never die on a pass
            logging.exception("reconcile failed: %s", e)
            return None

    def stop(self):
        """Sweep before anything else, then stop.

        Both DAY and GTC synthetics are cancelled here and do not resume
        on restart: while this process is down nobody is watching the
        spread, so an order that "survived" would be a promise nothing
        could keep (spec §3.1).
        """
        self._stop.set()
        self.book.cancel_where(reason='the system stopped — synthetic orders '
                                      'do not survive a restart')
        return self.sweep_pendings('shutdown')


def _offered(leg, symbol):
    """What this account actually has that resembles the symbol asked for.

    Brokers spell gold XAUUSD, GOLD, XAUUSD.r and a future GC1226 or
    GCZ4, so the search walks the stem back — `GCZ4`, `GCZ`, `GC` —
    rather than failing on an exact miss. Listing something is the whole
    point: a bare "not found" leaves the operator guessing at spellings.
    """
    stem = ''.join(c for c in (symbol or '') if c.isalnum())
    for length in range(len(stem), 1, -1):
        found = leg.find_symbols(stem[:length]) or []
        if found:
            return found
    return leg.find_symbols('') or []


def _meta_from_report(report):
    """The subset of MT5's symbol facts the sizing and ladder math read."""
    bid, ask = report.get('bid'), report.get('ask')
    return {
        'symbol': report.get('symbol'),
        'description': report.get('description'),
        'contract_size': report.get('contract_size'),
        'tick_size': report.get('tick_size'),
        'tick_value': report.get('tick_value'),
        'digits': report.get('digits'),
        'point': report.get('point'),
        'volume_min': report.get('volume_min'),
        'volume_max': report.get('volume_max'),
        'volume_step': report.get('volume_step'),
        'filling_mode': report.get('filling_mode'),
        'trade_allowed': report.get('trade_allowed'),
        'bid': bid, 'ask': ask,
        'mid': ((bid + ask) / 2.0) if (bid and ask) else None,
        # Measured, and shown on the ladder: which leg quotes should be
        # decided from the widths, not assumed (spec §4).
        'width': ((ask - bid) if (bid and ask) else None),
    }


def _badge(md, stale, jumped):
    """The feed badge: OK / oldest leg 2.3s / desynced."""
    if jumped:
        return 'desynced'
    if stale:
        return 'stale'
    ages = [md.get('leg_a_quote_age_sec'), md.get('leg_b_quote_age_sec')]
    ages = [a for a in ages if a is not None]
    if not ages:
        return 'warming up'
    return f'OK (oldest leg {max(ages):.1f}s)'


def ladder_rows(pair, md, book, rows=None):
    """The rows the ladder draws: the spread +/- N increments.

    Generated here rather than in the browser so the ladder cannot
    disagree with the engine about where a level is — the Work column,
    the click that places an order and the price that names it all come
    off this one list.
    """
    increment = pair.effective_increment()
    if not md or not increment:
        return []
    rows = int(rows or pair.rows)
    mid = md['spread']
    centre = round(mid / increment) * increment
    out = []
    for step in range(rows, -rows - 1, -1):
        level = round(centre + step * increment, 10)
        buys, sells = book.working_at(pair.key, level)
        out.append({
            'level': level,
            'work_buy': buys or None,
            'work_sell': sells or None,
            # The inside market: the two levels the black rule sits
            # between (spec §3.3).
            'is_best_bid': _at(level, md['short_spread'], increment),
            'is_best_ask': _at(level, md['long_spread'], increment),
        })
    return out


def _at(level, price, increment):
    """Is this row the one that price falls in?"""
    if price is None:
        return False
    return abs(level - round(price / increment) * increment) < increment / 2


