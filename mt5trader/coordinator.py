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
from datetime import datetime

from . import algo as algo_module, atomicfile, carry, \
    config as config_module, depth as depth_book, fairvalue, hedgeratio, \
    sizing, takeprofit
from .book import Book
from .database import Store
from .executor import PairExecutor, mark_position
from .models import (MAGIC_NUMBER, LegFill, OrderType, SpreadPosition,
                     SpreadSide)
from .quoter import Quoter
from .reconcile import Reconciler
from .session import SessionClock, day_orders, overnight_action
from .spread import (LevelSigma, QuoteAgeTracker, SpreadJumpTracker,
                     compute_spread, stale_quote)


#: Settings whose only consumer is a panel or a display rule, so a save
#: takes effect on the next poll rather than at the next restart.
HOT_SETTINGS = (
    'TP_TARGET_PCT_OF_MARGIN', 'SLIPPAGE_ALLOWANCE', 'BREAK_EVEN_NIGHTS',
    'CARRY_RATE_PCT', 'COMMISSION_PER_LOT_A', 'COMMISSION_PER_LOT_B',
    'SPREAD_COST_FACTOR',
)


class Coordinator:
    def __init__(self, config, legs, status_path='status.json',
                 clock=time.time, sleep=time.sleep,
                 monotonic=time.monotonic, store=None, leg_factory=None):
        self.config = config
        self.legs = legs                    # {account name: leg}
        #: Called with an account name to try, ONCE, to bring up a leg
        #: that was not there at startup. None = never retry (the
        #: tests, and anything driving the legs itself).
        self.leg_factory = leg_factory
        self._last_leg_try = None
        self.status_path = status_path
        self.clock = clock
        self.sleep = sleep
        self.book = Book()
        self.executor = PairExecutor(config, legs, clock=clock, sleep=sleep)
        self.quoter = Quoter(config, legs, self.executor, self.book)
        # Whoever closes a position — the trader, the overnight rule,
        # a flatten, the kill — pulls its resting take-profit first.
        self.executor.before_close = self.quoter.disarm
        self.reconciler = Reconciler(config, legs, self.book, self.executor,
                                     clock=clock)
        self._last_reconcile = None
        self.session_clock = SessionClock(config, offset=self.broker_offset)
        #: The measured broker clock, per account: (measured at, offset).
        #: Re-measured slowly — a broker's clock does not drift on the
        #: scale of a poll, and it is a round trip per account.
        self._offsets = {}
        #: The local database: crash-safe position state, and the fill
        #: journal. None means run without one — the tests that do not
        #: care, and nothing else.
        self.store = store
        #: What recovery found at startup, and what it could not claim.
        self.recovery = {'recovered': 0, 'unclaimed': [], 'complete': False}
        self._last_journal = None
        #: Set by the launcher: the bridge from the web process. Primed
        #: at startup, so a restart never replays a command history.
        self.commands = None
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
        #: (account, symbol) -> (at, the leg's own session figures)
        self._session_cache = {}
        #: (account, symbol) -> (at, that leg's DOM)
        self._depth_cache = {}
        #: pair key -> (at, margin for ONE spread across both accounts)
        self._margin_cache = {}
        #: (mtime, size) of the config last read, so the hot-apply
        #: watcher opens the file only when it has actually changed.
        self._config_marker = None
        #: pair key -> the price its ladder window is anchored on. Held
        #: still between polls so a row keeps its price; see
        #: `ladder_anchor`.
        self._ladder_anchor = {}
        #: Pairs whose ladder the trader has LOCKED. A locked ladder
        #: does not re-anchor and does not widen its window to follow
        #: the touches: the price at every row stays where it is until
        #: they press Centre. See `_anchor_for` and `ladder_rows`.
        self._ladder_locked = {}
        #: pair key -> when a stale leg was last written to the log
        self._stale_logged = {}
        #: pair key -> when a stale pair last re-subscribed itself
        self._auto_refreshed = {}
        self._loop_interval = None
        self._last_poll = None
        self._stop = threading.Event()
        #: One lock over the book and the executor. Commands arrive on
        #: their own thread — a click must not wait for the next poll —
        #: and a poll reading the book while a click mutates it would
        #: publish a half-placed order.
        self.lock = threading.RLock()
        #: Symbol resolution failures, in the operator's words, kept on
        #: the snapshot so a broken pair is VISIBLE rather than absent
        #: (spec §17: never hide a broken row).
        self.errors = {}

    # -- startup ------------------------------------------------------------

    def start(self):
        """Sweep, resolve symbols, recover the book, then poll.

        The sweep is before ANYTHING else: a pending of ours still
        resting is from a previous life, and one that filled while we
        were down is an unhedged outright position nobody was watching.

        Recovery comes before the first reconcile for a sharper reason:
        the book starts EMPTY, and an empty book makes every real
        position look like an orphan. Sixty seconds later the reconciler
        would close them — the machinery working correctly on a book
        that lied to it.
        """
        swept = self.sweep_pendings('startup')
        self.resolve_symbols()
        self.recover()
        return swept

    def recover(self):
        """Bring back the positions that were open when we stopped.

        Each one comes back ACTIVE, with its own id, fills and tickets —
        a position recovered under a NEW id is an orphan to the
        reconciler and a ghost to the book.

        Then: anything at the broker carrying OUR magic that recovery
        did not account for is UNCLAIMED. It is not closed and it is not
        struck out; it is put on the screen for a person to adopt or
        close, because a position we cannot explain is exactly the one
        an automatic close should not touch.
        """
        report = {'recovered': 0, 'unclaimed': [], 'complete': False,
                  'error': None}
        if self.store is None:
            # No database: we cannot know what was open. Say so, and let
            # the reconciler hold its fire rather than guess.
            report['error'] = ('no database — open positions cannot be '
                               'recovered, so nothing at the broker will be '
                               'auto-closed')
            self.recovery = report
            self.reconciler.book_complete = False
            return report
        try:
            for row in self.store.open_positions():
                position = SpreadPosition.from_dict(row)
                self.book.add_position(position)
                report['recovered'] += 1
        except Exception as e:                       # a broken database
            logging.critical('could not recover positions: %s', e)
            report['error'] = str(e)
            self.recovery = report
            self.reconciler.book_complete = False
            return report

        known = self.reconciler.known_tickets()
        for name, leg in self.legs.items():
            positions = leg.positions()
            if positions is None:
                # UNKNOWN, not flat: we cannot complete recovery against
                # an account we could not read.
                report['error'] = (f"account '{name}' could not be read at "
                                   f"startup — recovery is incomplete")
                continue
            for position in positions:
                if (name, str(position['ticket'])) in known:
                    continue
                report['unclaimed'].append(dict(position, account=name))

        for name in self.dark_accounts():
            # An account with no runner is UNKNOWN, not flat. Saying
            # recovery is complete here would licence the reconciler to
            # auto-close positions on the accounts it CAN read, on the
            # strength of a book missing everything on the one it
            # cannot.
            report['error'] = (f"account '{name}' has no leg runner — "
                               f"recovery is incomplete")
        report['complete'] = report['error'] is None
        # The reconciler auto-closes orphans only when the book is known
        # to be complete. It never auto-closes anything on this list.
        self.reconciler.book_complete = report['complete']
        self.reconciler.unclaimed = {
            (row['account'], str(row['ticket'])): row
            for row in report['unclaimed']}
        if report['recovered']:
            logging.warning('recovered %d open position(s) from the database',
                            report['recovered'])
        if report['unclaimed']:
            logging.critical(
                '%d position(s) at the broker carry our magic but are not in '
                'our book. They will NOT be closed automatically — adopt or '
                'close them by hand: %s', len(report['unclaimed']),
                ', '.join(f"{row['account']}:{row['ticket']} {row['symbol']} "
                          f"{row['side']} {row['volume']}"
                          for row in report['unclaimed']))
        if self.store is not None:
            self.store.event('recovery', **{k: v for k, v in report.items()
                                            if k != 'unclaimed'})
        self.recovery = report
        return report

    def dark_accounts(self):
        """Configured accounts whose leg runner is not answering.

        Published, because this is the difference between "the market
        is quiet" and "half the engine is not there" — and the screen
        must never have to guess which.
        """
        return [name for name in self.config.accounts
                if name not in self.legs]

    def retry_dark_legs(self):
        """Try, on a slow clock, to attach the accounts that are dark.

        A leg runner started late — or restarted at lunchtime — used to
        need the whole engine restarted with it. Here it just joins:
        its pendings are swept (magic-scoped: they are from a previous
        life and one still resting can fill unhedged), its symbols are
        resolved, and the book is recovered against it before the
        reconciler is allowed to call itself complete.
        """
        if self.leg_factory is None:
            return []
        dark = self.dark_accounts()
        if not dark:
            return []
        every = float(self.config.get('LEG_RETRY_SEC', 5.0))
        now = self.clock()
        if self._last_leg_try is not None and now - self._last_leg_try < every:
            return []
        self._last_leg_try = now
        joined = []
        for name in dark:
            try:
                leg = self.leg_factory(name)
            except Exception as e:                 # a runner mid-start
                logging.warning("account '%s': leg runner did not answer "
                                "(%s)", name, e)
                continue
            if leg is None:
                continue
            with self.lock:
                self.legs[name] = leg
                self.sweep_pendings(f'join:{name}', legs={name: leg})
                self.resolve_symbols()
                self.recover()
            joined.append(name)
            logging.warning(
                "account '%s' joined: its leg runner answered and its "
                "symbols were resolved", name)
        return joined

    def sweep_pendings(self, when, legs=None):
        """Cancel every pending of OURS on both accounts, and verify.

        Magic-scoped, so the trader's own terminal orders are never
        touched. A pending we failed to cancel is a CRITICAL line, not a
        warning — it can fill unhedged with nobody watching.
        """
        report = {'when': when, 'cancelled': [], 'failed': [], 'unknown': []}
        for name, leg in (self.legs if legs is None else legs).items():
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
        with self.lock:
            return self._poll_once()

    def reload_reference_fields(self):
        """Re-read the config and take the REFERENCE-ONLY pair fields.

        Symbols, contract sizes and beta are structural and need a
        restart; four fields whose only consumer is a panel are not. In
        the system this is ported from they were blocked behind the
        structural keys, and the log said "an assets change requires a
        restart" ten lines above a live trade while the values sat
        saved and correct.

        Reads only when the file has actually CHANGED, and logs only
        what actually moved — a watcher that prints every poll is a
        watcher nobody reads.
        """
        path = getattr(self.config, 'path', None)
        if not path:
            return []
        try:
            stamp = os.stat(path)
            marker = (stamp.st_mtime, stamp.st_size)
        except OSError:
            return []
        if marker == self._config_marker:
            return []
        self._config_marker = marker
        try:
            raw = config_module.load_raw(path)
        except Exception as e:                # a half-written config
            logging.debug('config re-read: %s', e)
            return []
        moved = []
        for key, pair in self.config.pairs.items():
            changed = pair.apply_hot((raw.get('pairs') or {}).get(key) or {})
            if changed:
                moved.append(f"{key}: {', '.join(changed)}")
        settings = raw.get('settings') or {}
        for key in HOT_SETTINGS:
            if key in settings and self.config.settings.get(key) != \
                    settings[key]:
                self.config.settings[key] = settings[key]
                moved.append(key)
        if moved:
            logging.info('applied without a restart — %s', '; '.join(moved))
        return moved

    def _poll_once(self):
        started = self.clock()
        self.reload_reference_fields()
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
            if stale:
                self._log_stale(key, pair, md)
                self._auto_refresh(key, md)
            md['jump_reason'] = jumped
            md['guard_reason'] = stale or jumped
            # The badge the ladder shows continuously, so a trader can
            # see what they are clicking into (spec §8).
            md['feed_badge'] = _badge(md, stale, jumped)
            self._observe_session(key, md, pair)
            self.market[key] = md
            # LIMIT-mode orders are worked on the SAME pass that priced
            # them: a peg re-priced off a snapshot older than the one on
            # screen is a peg holding a level nobody is showing.
            self.quoter.work(pair, md)
            if any(e['action'] == 'auto_route_armed'
                   for e in self.work_auto_route(pair, md)):
                # A target armed on THIS pass rests on this pass. A poll
                # later is a window where the trader believes there is a
                # working order and there is not.
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

    def refresh_feed(self, pair_key):
        """Take both legs out of Market Watch and put them back.

        The answer to "the price is moving in MT5 and this says stale":
        a re-subscribe is what restarts a feed the terminal has gone
        quiet on, and it is a thing the trader can DO rather than a
        thing to wait out. The staleness clock is reset with it — not
        to hide the age, but because the old age was measured against a
        subscription that no longer exists.

        It reports the prices that came back, so the screen can say
        whether it worked instead of claiming it did.
        """
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        got = {}
        for leg_name, account, symbol in (('A', pair.account_a, pair.symbol_a),
                                          ('B', pair.account_b,
                                           pair.symbol_b)):
            leg = self.legs.get(account)
            if leg is None or not symbol:
                got[leg_name] = None
                continue
            try:
                got[leg_name] = leg.resubscribe(symbol)
            except Exception as e:
                got[leg_name] = None
                logging.error('refresh %s: %s', symbol, e)
        with self.lock:
            self.quote_ages.forget(pair_key)
            if hasattr(self.jumps, 'forget'):
                self.jumps.forget(pair_key)
        alive = [name for name, tick in got.items() if tick]
        return {'ok': bool(alive),
                'reason': ('re-subscribed ' +
                           ', '.join(f"leg {name} "
                                     f"{(got[name] or {}).get('bid')}"
                                     f"/{(got[name] or {}).get('ask')}"
                                     for name in ('A', 'B') if got.get(name))
                           if alive else
                           'neither leg answered — check the terminals'),
                'ticks': got}

    def margin_per_spread(self, pair):
        """What ONE spread ties up, in money, across BOTH accounts.

        Priced by the terminals themselves — margin depends on the
        broker's leverage, the symbol's margin mode and the account
        group, so a number computed here would be a guess wearing a
        figure's clothes. Cached: it moves with the price, but slowly.

        None when either leg cannot price it. The take-profit target
        then shows an em dash and says why, rather than being built on
        half a number.
        """
        return (self.margin_detail(pair) or {}).get('money')

    def margin_detail(self, pair):
        """What one spread ties up, WITH where the figure came from.

        The terminals price it themselves (`order_calc_margin`), which
        is the honest number: margin depends on the broker's leverage,
        the symbol's margin mode and the account group. Where a leg
        cannot price it, each leg's notional over ITS OWN account's
        leverage is the fallback — not one leverage for both — and it
        is LABELLED, because a target computed off the wrong base looks
        like a considered number.

        No leverage either, and there is NO suggestion: a target on a
        base nobody set is worse than no target.
        """
        ttl = float(self.config.get('MARGIN_TTL_SEC', 60.0))
        now = self.clock()
        cached = self._margin_cache.get(pair.key)
        if cached and now - cached[0] < ttl:
            return cached[1]
        detail = self._price_margin(pair)
        self._margin_cache[pair.key] = (now, detail)
        return detail

    def _price_margin(self, pair):
        total = 0.0
        source = 'terminal'
        for meta, account, symbol, lots, side in (
                (pair.meta_a, pair.account_a, pair.symbol_a,
                 pair.clip_lots_a, 'SELL'),
                (pair.meta_b, pair.account_b, pair.symbol_b,
                 pair.clip_lots_b, 'BUY')):
            leg = self.legs.get(account)
            if leg is None or not symbol or not lots:
                total = None
                break
            try:
                margin = leg.margin_for(symbol, side, lots)
            except Exception:                 # a leg mid-restart
                margin = None
            if margin is None:
                margin = self._margin_from_leverage(account, meta, lots)
                if margin is not None:
                    source = 'leverage'
            if margin is None:
                total = None
                break
            total += float(margin)
        if total is None:
            return {'money': None, 'source': None,
                    'note': 'the terminals have not priced the margin for '
                            'one spread, and no account leverage is known — '
                            'set leverage to size a % target'}
        return {'money': total, 'source': source,
                'note': ('priced by the terminals themselves'
                         if source == 'terminal' else
                         "each leg's notional over its own account's "
                         "leverage — the terminals could not price it")}

    def _margin_from_leverage(self, account, meta, lots):
        """Notional over THAT account's leverage. Not one for both."""
        info = self.account_info(account) or {}
        leverage = info.get('leverage')
        price = (meta or {}).get('ask') or (meta or {}).get('bid')
        contract = (meta or {}).get('contract_size')
        if not leverage or not price or not contract:
            return None
        return float(lots) * float(contract) * float(price) / float(leverage)

    def fair_spread(self, pair, md):
        """What the CARRY says this basis should be, both directions.

        Priced from the broker's OWN swap and the nights to expiry —
        `-carry / k`, which is size-free — and compared against the
        price each direction would actually trade at, never a midpoint.

        ONE path to the carry: the broker's swap per leg per side,
        with the four typed fields as overrides. There used to be a
        second — a single number in spread points per day that
        short-circuited all of it — and two ways to price the same
        carry is two answers to reconcile when the reading looks
        wrong.
        """
        now = self.session_clock.broker_now() or datetime.now()
        expiry = pair.effective_expiry('b')
        # A CALENDAR is decided when its near leg expires, so that is
        # where the carry runs to. Running it to the far leg prices a
        # trade that is already over.
        kind = algo_module.pair_kind(pair, pair.effective_expiry('a'), expiry)
        nights, kind_note = algo_module.carry_nights(
            kind,
            fairvalue.days_to_expiry(pair.effective_expiry('a'), now),
            fairvalue.days_to_expiry(expiry, now))
        body = carry.describe(
            pair.meta_a, pair.meta_b, pair.hedge_ratio,
            pair.clip_lots_a, pair.clip_lots_b,
            sizing.spread_units(pair.clip_lots_b,
                                (pair.meta_b or {}).get('contract_size')),
            nights, market=md, overrides=pair.swap_overrides(),
            expects_expiry=(kind != algo_module.RELATED),
            rate_pct=pair.exit_settings(
                self.config.settings).get('CARRY_RATE_PCT'))
        body['kind'] = kind
        body['kind_note'] = kind_note
        return body

    def work_auto_route(self, pair, md):
        """Arm — and keep honest — the closing orders AutoRouting rests.

        On a fill it rests a working order to close the position at its
        take-profit, priced from the ACTUAL EXECUTED spread and not from
        the quote the click was taken at. That anchor is the whole
        point: levels anchored on the quote while P&L is measured from
        the fill name a level the engine would not fire at.

        It arms a TARGET AND NO STOP. The position runs until the
        target, the overnight rule, or the trader.

        On a restart it re-arms from the recovered position's frozen
        levels, and SAYS SO. That deliberately differs from the rule
        that nothing placing orders by itself may resume after a
        restart: that rule exists because a replayed ENTRY creates risk
        nobody chose. This places a CLOSING order on a position that
        already exists — it reduces exposure — and the worse failure
        here is silent: a trader who believes a target is armed when it
        is not.
        """
        events = []
        # First, the housekeeping that must happen whether AutoRouting
        # is on or off: an order armed against a position that has gone
        # is pulled. It would fill, and with nothing to close it would
        # OPEN a naked position.
        live = {p.position_id for p in self.book.positions(pair.key)}
        for order in self.book.orders(pair.key):
            if order.position_id and order.position_id not in live:
                self.quoter.disarm(order.position_id,
                                   'its position is no longer open')
                events.append({'pair': pair.key, 'action': 'auto_route_pulled',
                               'position': order.position_id})
        # The master switch, and then the ladder's own. OFF at either
        # level, nothing is armed — AND anything already resting is
        # PULLED: standing automation down that leaves an order behind
        # it has stood nothing down, and that order still fills.
        on = (self.config.get('AUTO_ROUTE_ENABLED', False)
              and pair.auto_route)
        if not on:
            for position in self.book.positions(pair.key):
                if self.book.orders_for_position(position.position_id):
                    self.quoter.disarm(position.position_id,
                                       'AutoRouting was turned off')
                    position.tp_armed = False
                    events.append({'pair': pair.key,
                                   'action': 'auto_route_pulled',
                                   'position': position.position_id})
            if events:
                self.session_events.extend(events)
            return events
        margin = (self.margin_detail(pair) or {}).get('money')
        # This ladder's own numbers: a gold basis and an oil differential
        # are charged differently and held for different lengths of time.
        settings = pair.exit_settings(self.config.settings)
        nights = float(settings.get('BREAK_EVEN_NIGHTS', 0.0) or 0.0)
        for position in self.book.positions(pair.key):
            if getattr(position, 'tp_armed', False):
                continue
            if self.book.orders_for_position(position.position_id):
                continue
            exit_levels = takeprofit.for_position(
                position, md, pair, settings, margin,
                nights=nights,
                carry_for=self.holding_carry(pair, position.side.value,
                                             nights))
            level = (exit_levels or {}).get('tp')
            # Break-even is not a target. With no margin priced, or no
            # percentage set, `tp` IS break-even — and resting an order
            # there is a different instruction from the one the trader
            # gave.
            if level is None or not (exit_levels or {}).get('target_points'):
                continue
            order = self.quoter.arm(pair, position, level)
            if order is None:
                continue
            position.tp_armed = True
            events.append({'pair': pair.key, 'action': 'auto_route_armed',
                           'position': position.position_id, 'level': level,
                           'order': order.order_id,
                           # Said out loud, because a target believed to
                           # be armed when it is not is the worse fault.
                           'recovered': bool(position.recovered)})
        if events:
            self.session_events.extend(events)
        return events

    def algo_block(self, pair, md, exit_levels=None):
        """What the selected algo says. NONE says nothing at all.

        Nothing in here places an order, and nothing in here is
        consulted by the click path: an algo is a reading beside the
        market, exactly like the fair spread was before it had a name.
        """
        selected = pair.algo or algo_module.NONE
        body = {'algo': selected, 'window': pair.algo_window}
        if selected == algo_module.FAIR_SPREAD:
            body['fair'] = self.fair_spread(pair, md)
            body['kind'] = body['fair'].get('kind')
            body['kind_note'] = body['fair'].get('kind_note')
        return body

    def holding_carry(self, pair, direction, nights):
        """What holding one spread `nights` nights costs, in money.

        The same conversion the fair spread uses — and the same
        refusal: a swap MT5 reports in units this cannot read comes
        back as None with the reason, and break-even then says so
        rather than silently dropping the term.
        """
        if not nights:
            return {'money': 0.0, 'reason': None}
        return carry.carry_money(pair.meta_a, pair.meta_b, direction,
                                 pair.clip_lots_a, pair.clip_lots_b, nights,
                                 pair.swap_overrides())

    def leg_depth(self, account, symbol):
        """One leg's market depth, cached for a fraction of a second.

        A DOM is a round trip per leg per poll. Most CFD accounts
        publish none at all, and the broker remembers that refusal, so
        this costs nothing where there is nothing.
        """
        if not self.config.get('SHOW_DEPTH', True):
            return None
        ttl = float(self.config.get('DEPTH_TTL_SEC', 0.25))
        now = self.clock()
        cached = self._depth_cache.get((account, symbol))
        if cached and now - cached[0] < ttl:
            return cached[1]
        leg = self.legs.get(account)
        book = None
        if leg is not None and symbol:
            try:
                book = leg.depth(symbol)
            except Exception:                 # a leg mid-restart
                book = None
        self._depth_cache[(account, symbol)] = (now, book)
        return book

    def implied_depth(self, pair):
        """How many SPREADS each side of the two books can actually do.

        Empty when either broker publishes no depth: a size invented
        from one leg would show a hundred lots available on a spread
        that can do four, and it is a lie a trader would click on.
        """
        book_a = self.leg_depth(pair.account_a, pair.symbol_a)
        book_b = self.leg_depth(pair.account_b, pair.symbol_b)
        if not book_a or not book_b:
            return {'buy': {}, 'sell': {}, 'published': False}
        sizes = depth_book.implied(
            book_a, book_b, pair.hedge_ratio,
            clip_a=pair.clip_lots_a, clip_b=pair.clip_lots_b,
            increment=pair.effective_increment())
        sizes['published'] = True
        return sizes

    def leg_session(self, account, symbol):
        """One leg's own session O/H/L/V, cached: it is an IPC round
        trip for numbers that move slowly, and the ladder polls three
        times a second."""
        ttl = float(self.config.get('SESSION_STATS_TTL_SEC', 2.0))
        now = self.clock()
        cached = self._session_cache.get((account, symbol))
        if cached and now - cached[0] < ttl:
            return cached[1]
        leg = self.legs.get(account)
        stats = None
        if leg is not None and symbol:
            try:
                stats = leg.session_stats(symbol)
            except Exception:                 # a leg mid-restart
                stats = None
        self._session_cache[(account, symbol)] = (now, stats)
        return stats

    def _auto_refresh(self, key, md):
        """Re-subscribe a stale pair by itself, now and then.

        Some terminals drop a symbol's subscription silently: the
        ladder ticks for a few seconds after a refresh and then goes
        quiet again, and pressing Feed brings it back every time. A
        machine that needs the same button pressed every twenty seconds
        should press it itself.

        Logged on every attempt. A feed being nursed along is a fact
        the operator needs — nursing it quietly would hide exactly the
        problem worth fixing at the broker or the terminal.
        """
        every = float(self.config.get('AUTO_REFRESH_STALE_SEC', 20.0) or 0)
        if not every:
            return
        now = self.clock()
        if now - self._auto_refreshed.get(key, 0.0) < every:
            return
        self._auto_refreshed[key] = now
        answer = self.refresh_feed(key)
        logging.warning('%s: stale — re-subscribed both legs automatically '
                        '(%s). If this repeats, the terminal is dropping the '
                        'subscription: add both symbols to Market Watch and '
                        'leave them there.', key, answer.get('reason'))

    def _log_stale(self, key, pair, md):
        """Say what a frozen leg actually looks like, once a minute.

        The two faults are indistinguishable on the screen and have
        different answers: a symbol that is NOT VISIBLE is not
        subscribed, and re-subscribing fixes it; a visible symbol whose
        broker stamp is not advancing is a terminal receiving nothing,
        and the answer is in MT5. Printing both, with the stamps, turns
        "it goes stale again" into a fact somebody can act on.
        """
        every = float(self.config.get('STALE_LOG_EVERY_SEC', 60.0))
        now = self.clock()
        if now - self._stale_logged.get(key, 0.0) < every:
            return
        self._stale_logged[key] = now
        for leg, symbol in (('a', pair.symbol_a), ('b', pair.symbol_b)):
            logging.warning(
                "%s leg %s (%s): visible=%s broker stamp=%s last change "
                "%.1fs ago", key, leg.upper(), symbol,
                md.get(f'leg_{leg}_visible'),
                md.get(f'leg_{leg}_tick_time'),
                md.get(f'leg_{leg}_quote_age_sec') or 0.0)

    def _observe_session(self, key, md, pair=None):
        """The session line above the ladder.

        Taken from the two LEGS' own session figures where the terminals
        publish them — H is `high B - beta x high A`, the same
        convention as every other price on this screen — and from our
        own mid series where they do not. Which of the two it is, is
        said on the strip: a borrowed number read as the exchange's is
        how a spread gets judged against a range nobody traded.

        Volume stays PER LEG, never added: one lot of gold and one lot
        of the future are not two lots of anything, and a combined
        figure would be a number with no unit.
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
        published = dict(session)

        if pair is not None:
            beta = float(pair.hedge_ratio or 1.0)
            stats_a = self.leg_session(pair.account_a, pair.symbol_a) or {}
            stats_b = self.leg_session(pair.account_b, pair.symbol_b) or {}

            def combined(field):
                first, second = stats_a.get(field), stats_b.get(field)
                if first is None or second is None:
                    return None
                return second - beta * first

            legs_high = combined('high')
            legs_low = combined('low')
            legs_open = combined('open')
            if legs_high is not None or legs_low is not None:
                published.update({
                    'high': legs_high, 'low': legs_low,
                    'open': legs_open if legs_open is not None
                    else session['open'],
                    'ours': False,
                })
            published['volume_a'] = stats_a.get('volume')
            published['volume_b'] = stats_b.get('volume')
            published['legs'] = {'a': stats_a or None, 'b': stats_b or None}

        md['session'] = published
        # Net change is always against OUR open — the one the trader has
        # been watching this process quote.
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
        with self.lock:
            return self._click(pair_key, side, level, quantity)

    def _click(self, pair_key, side, level, quantity=None):
        pair = self.config.pairs.get(pair_key)
        if pair is None:
            return {'ok': False, 'reason': f'no pair {pair_key}'}
        shared = self.legs_share_an_account(pair)
        if shared and self.config.get('REFUSE_SHARED_ACCOUNT', False):
            # OFF by default. Two accounts on one terminal is usually a
            # misconfiguration, but "both legs on one account" is also
            # an ordinary spread, and which of the two it is, is the
            # trader's to say — so the screen says it loudly and this
            # refuses only when the desk has asked it to.
            return {'ok': False, 'refused': True,
                    'reason': (
                        f"'{pair.account_a}' and '{pair.account_b}' are "
                        f"both attached to MT5 login #{shared}, and "
                        f"REFUSE_SHARED_ACCOUNT is on.")}

        md = self.market.get(pair_key)
        quantity = pair.default_quantity if quantity is None else quantity

        if pair.order_type is OrderType.MARKET and \
                self._away_from_the_market(pair, side, md, level):
            # A ladder click away from the touch is an order to WORK,
            # not an order to refuse. In MARKET mode the trader is
            # saying "cross now" — but a buy under the offer cannot
            # cross now at any price, and the only honest readings are
            # "rest it here" or "do nothing". TT rests it, and so does
            # this; the toast says which it did, because a market click
            # that quietly became a working order is a surprise.
            order = self.book.add_order(pair, side, level, quantity)
            self.quoter.group_for(pair, order)
            return {'ok': True, 'order': order.to_dict(), 'rested': True,
                    'reason': f'{level:g} is away from the market — '
                              f'resting here as a working order'}

        if pair.order_type is OrderType.MARKET:
            result = self.executor.market_entry(pair, side, md, quantity,
                                                level)
            if result.ok and result.position:
                self.remember(self.book.add_position(result.position))
            elif self.store is not None and result.reason:
                self.store.event('refused', pair_key, reason=result.reason,
                                 side=getattr(side, 'value', side),
                                 level=level, naked=result.naked)
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

    def _away_from_the_market(self, pair, side, md, level):
        """Is this click at a price the market cannot fill right now?

        A BUY under the offer and a SELL over the bid are the two, and
        they are the ordinary way a ladder is used: click where you want
        to be, and wait there. Neither is an error, and refusing them
        was making the ladder's whole left-hand side dead.

        Unknown prices are NOT "away": with no market the executor's own
        refusal is the right answer, and it names what is missing.
        """
        if not self.config.get('CLICK_AWAY_RESTS', True):
            return False
        if not md or level is None:
            return False
        side = SpreadSide(getattr(side, 'value', side))
        increment = pair.effective_increment() or 0.0
        edge = increment / 2.0
        if side is SpreadSide.BUY:
            offer = md.get('long_spread')
            return offer is not None and level < offer - edge
        bid = md.get('short_spread')
        return bid is not None and level > bid + edge

    def cancel_order(self, order_id):
        with self.lock:
            return self._cancel_order(order_id)

    def _cancel_order(self, order_id):
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
        with self.lock:
            return self._cancel_where(pair_key, side, reason)

    def _cancel_where(self, pair_key=None, side=None, reason='cancelled'):
        pulled = self.book.cancel_where(pair_key, side, reason)
        for key, pair in self.config.pairs.items():
            if pair_key in (None, key):
                self.quoter.work(pair, self.market.get(key))
        return pulled

    def close_unclaimed(self, account, ticket):
        """Close one unexplained position at the broker, by ticket.

        Only ever because a person asked. The volume comes from the
        broker's own book, and a ticket MT5 no longer lists is already
        gone rather than an error.
        """
        with self.lock:
            row = self.reconciler.unclaimed.get((account, str(ticket)))
            if row is None:
                return {'ok': False, 'reason': f'{account}:{ticket} is not '
                                               f'on the unclaimed list'}
            leg = self.legs.get(account)
            if leg is None:
                return {'ok': False, 'reason': f"no leg runner for {account}"}
            result = self.executor.close_tickets(
                leg, row['symbol'], [ticket], row['side'],
                comment='UNCLAIMED')
            if result.get('ok'):
                self.reconciler.unclaimed.pop((account, str(ticket)), None)
                if self.store is not None:
                    self.store.event('unclaimed_closed', None, account=account,
                                     ticket=str(ticket), symbol=row['symbol'],
                                     volume=row['volume'])
            return result

    def adopt_unclaimed(self, pair_key, ticket_a, ticket_b):
        """Take two unexplained tickets into the book as one spread.

        For the case the operator can see and we cannot: the two legs of
        a pair that went on before the database existed, or under a
        build that did not persist. Once adopted it is managed, marked
        and closable like any other position.
        """
        with self.lock:
            pair = self.config.pairs.get(pair_key)
            if pair is None:
                return {'ok': False, 'reason': f'no pair {pair_key}'}
            rows = {}
            for leg_key, ticket, account in (
                    ('a', ticket_a, pair.account_a),
                    ('b', ticket_b, pair.account_b)):
                row = self.reconciler.unclaimed.get((account, str(ticket)))
                if row is None:
                    return {'ok': False,
                            'reason': f'{account}:{ticket} is not on the '
                                      f'unclaimed list'}
                rows[leg_key] = row

            # The SIDE of the spread comes from what leg B actually is:
            # buying the spread is long B, and a position that says
            # otherwise is not this pair's.
            side = (SpreadSide.BUY if rows['b']['side'] == 'BUY'
                    else SpreadSide.SELL)
            leg_a_side, leg_b_side = side.leg_sides()
            if rows['a']['side'] != leg_a_side.value:
                return {'ok': False,
                        'reason': (f"those two are not a hedge: leg B is "
                                   f"{rows['b']['side']} so leg A must be "
                                   f"{leg_a_side.value}, and it is "
                                   f"{rows['a']['side']}")}

            contract_a = (pair.meta_a or {}).get('contract_size')
            contract_b = (pair.meta_b or {}).get('contract_size')
            leg_a = LegFill(pair.account_a, rows['a']['symbol'], leg_a_side,
                            rows['a']['volume'], rows['a']['price_open'],
                            position_tickets=[rows['a']['ticket']],
                            contract_size=contract_a, clock=self.clock)
            leg_b = LegFill(pair.account_b, rows['b']['symbol'], leg_b_side,
                            rows['b']['volume'], rows['b']['price_open'],
                            position_tickets=[rows['b']['ticket']],
                            contract_size=contract_b, clock=self.clock)
            entry = (leg_b.price - float(pair.hedge_ratio) * leg_a.price
                     if (leg_a.price and leg_b.price) else None)
            quantity = (leg_b.volume / pair.clip_lots_b
                        if pair.clip_lots_b else leg_b.volume)
            position = SpreadPosition(
                pair_key, side, quantity, leg_a, leg_b, entry,
                OrderType.MARKET,
                sizing.spread_units(leg_b.volume, contract_b),
                clock=self.clock)
            position.recovered = True
            self.remember(self.book.add_position(position))
            for leg_key, ticket, account in (
                    ('a', ticket_a, pair.account_a),
                    ('b', ticket_b, pair.account_b)):
                self.reconciler.unclaimed.pop((account, str(ticket)), None)
            if self.store is not None:
                self.store.event('adopted', pair_key,
                                 position_id=position.position_id,
                                 tickets=[str(ticket_a), str(ticket_b)])
            return {'ok': True, 'position': position.to_dict()}

    def remember(self, position):
        """Write a position through to the database.

        On every change, not on a timer: the window between an order
        filling and the state being safe is the window a crash turns
        into an unrecoverable position.
        """
        if self.store is not None and position is not None:
            try:
                self.store.save_position(position)
            except Exception as e:                   # never lose the trade
                logging.critical('could not persist %s: %s — a restart will '
                                 'not recover it', position.position_id, e)
        return position

    def remember_all(self):
        for position in self.book.positions(open_only=False):
            self.remember(position)

    def account_health(self):
        """The margin picture, per account, and which one is weakest.

        With two brokers there is no such thing as combined margin: each
        posts its own, and a pair can only be carried by the WEAKER of
        the two. A total would read comfortable while one side is at its
        stop-out level.

        Our own exposure is stated beside the broker's numbers in the
        units the operator sized in — leg lots and units — because a
        margin figure with nothing to compare it against is a number
        nobody can act on.
        """
        warn_at = float(self.config.get('MARGIN_WARN_LEVEL', 200.0) or 0.0)
        rows = {}
        for name in self.legs:
            info = self.account_info(name) or {}
            equity = info.get('equity')
            margin = info.get('margin') or 0.0
            level = info.get('margin_level')
            if level is None and margin and equity is not None:
                level = 100.0 * equity / margin
            lots = units = 0.0
            positions = 0
            for position in self.book.positions():
                for fill in (position.leg_a, position.leg_b):
                    if not fill or fill.account != name:
                        continue
                    lots += fill.volume
                    units += fill.volume * (fill.contract_size or 0.0)
                    positions += 1
            rows[name] = {
                'account': name,
                'known': bool(info),
                'login': info.get('login'), 'server': info.get('server'),
                'currency': info.get('currency'),
                'leverage': info.get('leverage'),
                'balance': info.get('balance'),
                'credit': info.get('credit'),
                'equity': equity,
                'profit': info.get('profit'),
                'margin': margin,
                'margin_free': info.get('margin_free'),
                'margin_level': level,
                'so_call': info.get('margin_so_call'),
                'so_so': info.get('margin_so_so'),
                'our_lots': lots, 'our_units': units,
                'our_legs': positions,
                # A level under the warn threshold, or under the
                # broker's own margin-call level, whichever is higher.
                'tight': bool(level is not None
                              and level < max(warn_at,
                                              info.get('margin_so_call') or 0)),
            }
        measured = [row for row in rows.values()
                    if row['margin_level'] is not None]
        weakest = min(measured, key=lambda row: row['margin_level']) \
            if measured else None
        return {
            'accounts': rows,
            'warn_level': warn_at,
            # TWO LEGS, ONE ACCOUNT. Two runners can both attach to the
            # same running terminal — a blank terminal path on both, or
            # one terminal open — and then every "hedge" is two orders
            # on one account: no spread, twice the exposure, and a
            # screen that looks perfectly normal. The logins are read
            # from the terminals themselves, so this catches it however
            # the config was written.
            'same_login': _same_login(rows),
            # The weakest account governs. Named, not averaged.
            'weakest': weakest['account'] if weakest else None,
            'weakest_level': weakest['margin_level'] if weakest else None,
            'unknown': [name for name, row in rows.items()
                        if not row['known']],
        }

    def legs_share_an_account(self, pair):
        """Two SEPARATE accounts that turned out to be one terminal.

        One account carrying both legs is perfectly ordinary — spot
        silver and the silver future at the same broker is a spread, and
        a hedging account holds both sides at once. That case is
        configured deliberately, by pointing both legs at the SAME
        account, and it trades here like any other.

        What this catches is the other thing, which looks identical on
        the screen and is not the same at all: two accounts configured
        as two, both attaching to one running terminal — a blank
        terminal path on both is enough. Then the config says "hedged
        across two brokers" while every order lands on one login, the
        margin is one pool nobody is watching, and two runners are
        fighting over one terminal. Entries are refused on that; closes
        and cancels are never touched.
        """
        if pair.account_a == pair.account_b:
            return None                      # deliberate, and supported
        first = (self.account_info(pair.account_a) or {}).get('login')
        second = (self.account_info(pair.account_b) or {}).get('login')
        if first and second and first == second:
            return first
        return None

    def _anchor_for(self, key, pair, md):
        """This pair's ladder anchor, moved only when it must be."""
        increment = pair.effective_increment()
        spread = (md or {}).get('spread')
        if not increment or spread is None:
            return self._ladder_anchor.get(key)
        held = self._ladder_anchor.get(key)
        # LOCKED means locked. The drift rule below is what a ladder
        # does when the trader has not said otherwise; once they have,
        # the window stays where they put it however far the market
        # walks, and Centre is how they get it back.
        if held is not None and self._ladder_locked.get(key):
            return held
        anchor = ladder_anchor(held, spread, increment, int(pair.rows))
        self._ladder_anchor[key] = anchor
        return anchor

    def lock_ladder(self, key, locked):
        """Hold this pair's price window still, or let it follow again.

        The Lock tick on the ladder. It used to stop only the BROWSER
        scrolling, which left the two movers that actually shift a
        price off its row — the anchor re-centring, and the window
        widening to cover the touches — running underneath it. A
        control named Lock that locks a third of the movement is worse
        than no control, because the trader believes the ladder is
        still.
        """
        self._ladder_locked[key] = bool(locked)
        return bool(locked)

    def recentre_ladder(self, key):
        """Drop the anchor so the next poll rebuilds around the market.

        The manual Recentre button, and what a pair falls back to when
        its market has walked out of the window entirely.
        """
        # Deliberately does NOT clear the lock: Centre means "show me
        # the market again", not "and start following it". A trader who
        # locked the ladder gets a window rebuilt around the market and
        # then held there.
        self._ladder_anchor.pop(key, None)

    def broker_offset(self):
        """Seconds the BROKER's clock runs ahead of this machine's.

        Measured from the terminals, never configured: a typed-in time
        zone goes stale at every daylight-saving change. Both accounts
        should agree; when they do not, the WORSE case is not meaningful
        here, so leg A's is used and the disagreement is published for
        the screen — two brokers on different clocks is a fact the
        operator needs, not something to average away.

        None when it has not been measured. The session cutoff then does
        not fire, and says so.
        """
        ttl = float(self.config.get('BROKER_CLOCK_TTL_SEC', 300.0))
        now = self.clock()
        offsets = []
        for name, leg in self.legs.items():
            cached = self._offsets.get(name)
            if cached is None or now - cached[0] > ttl:
                try:
                    measured = leg.server_offset()
                except Exception:                    # a leg mid-restart
                    measured = None
                if measured is not None or cached is None:
                    self._offsets[name] = (now, measured)
                    cached = self._offsets[name]
            if cached and cached[1] is not None:
                offsets.append(cached[1])
        if not offsets:
            return None
        return offsets[0]

    def broker_clock(self):
        """What the screen shows about which clock we are on."""
        block = self.session_clock.describe()
        block['per_account'] = {
            name: (cached[1] if cached else None)
            for name, cached in self._offsets.items()}
        measured = [value for value in block['per_account'].values()
                    if value is not None]
        # Two accounts whose clocks disagree by more than a minute is
        # worth saying out loud: it usually means two different brokers,
        # and the cutoff can only be on one of them.
        block['accounts_disagree'] = bool(
            measured and max(measured) - min(measured) > 60)
        return block

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
            sizes = self.implied_depth(pair)
            # Break-even is only DEFINED given a holding period: the
            # swap is charged per night. 0 is intraday, where the term
            # vanishes, and it is the default.
            settings = pair.exit_settings(self.config.settings)
            nights = float(settings.get('BREAK_EVEN_NIGHTS', 0.0) or 0.0)
            margin = self.margin_detail(pair) or {}
            # The pair's own range this session, so a target can be read
            # against what the spread actually travels.
            seen = self.session.get(key) or {}
            session_range = (None if seen.get('high') is None
                             or seen.get('low') is None
                             else seen['high'] - seen['low'])
            net, avg_entry = self.book.net_position(key)
            buys, sells = self.book.working_counts(key)
            positions = []
            open_pnl = 0.0
            for position in self.book.positions(key):
                gross, net_pnl, closing = mark_position(
                    position, md, settings)
                row = position.to_dict()
                row.update({'gross_pnl': gross, 'net_pnl': net_pnl,
                            'closing_spread': closing,
                            # Anchored on the price this position was
                            # ENTERED at: a take-profit that moves with
                            # the market is not a take-profit.
                            'exit': takeprofit.for_position(
                                position, md, pair, settings,
                                margin.get('money'),
                                nights=nights,
                                carry_for=self.holding_carry(
                                    pair, position.side.value, nights))})
                positions.append(row)
                # UNMEASURED IS NOT ZERO. A position that could not be
                # marked used to be skipped, so the total read as though
                # it contributed nothing — an authoritative-looking
                # number that was silently short one position. One
                # unmarkable position makes the TOTAL unknown.
                if net_pnl is None:
                    open_pnl = None
                elif open_pnl is not None:
                    open_pnl += net_pnl
            row_exit = takeprofit.describe(
                pair, md, settings,
                margin_per_spread=margin.get('money'),
                margin_source=margin.get('source'),
                session_range=session_range,
                quantity=pair.default_quantity,
                spread_units=sizing.spread_units(
                    pair.clip_lots_b,
                    (pair.meta_b or {}).get('contract_size')),
                nights=nights,
                carry_buy=self.holding_carry(pair, 'BUY', nights),
                carry_sell=self.holding_carry(pair, 'SELL', nights))
            pairs[key] = {
                'key': key, 'name': pair.name, 'enabled': pair.enabled,
                'account_a': pair.account_a, 'account_b': pair.account_b,
                'symbol_a': pair.symbol_a, 'symbol_b': pair.symbol_b,
                # What KIND of pair this is — the operator's own
                # declaration, and the only thing that says whether a
                # fair spread applies at all.
                'pair_type': pair.pair_type,
                'hedge_ratio': pair.hedge_ratio,
                'hedge_ratio_for': pair.hedge_ratio_for,
                'increment': pair.effective_increment(),
                'increment_derived': pair.derived_increment(),
                'order_type': pair.order_type.value,
                'time_in_force': pair.time_in_force.value,
                'overnight': pair.overnight.value,
                # AutoRouting: on a fill, rest a working order to close
                # at the take-profit, priced from the actual fill. A
                # target and NO stop — the panel says so in words.
                'auto_route': pair.auto_route,
                # What the ladder's tick actually AMOUNTS to. The
                # master switch is off by default, and a title bar
                # reading AUTO off the pair's box alone said a fill
                # would arm a target when nothing would.
                'auto_route_on': bool(
                    pair.auto_route
                    and self.config.get('AUTO_ROUTE_ENABLED', False)),
                'auto_route_master': bool(
                    self.config.get('AUTO_ROUTE_ENABLED', False)),
                # The selected algo, and what it says. NONE publishes
                # only its own name — a ladder running nothing costs
                # nothing on the wire either.
                'algo': pair.algo,
                'algo_window': pair.algo_window,
                'algo_block': self.algo_block(pair, md, row_exit),
                'show_fair_window': pair.algo_window,   # the old name
                'auto_route_armed': [
                    {'position_id': o.position_id, 'level': o.level,
                     'order_id': o.order_id, 'quantity': o.remaining}
                    for o in self.book.orders(key) if o.position_id],
                'default_quantity': pair.default_quantity,
                # How many price levels the ladder draws. Named apart
                # from `rows`, which is the rows THEMSELVES — one of
                # them is a count and the other is a list, and a
                # settings pane reading the wrong one shows a form full
                # of prices.
                'row_count': pair.rows,
                'clip_lots_a': pair.clip_lots_a,
                'clip_lots_b': pair.clip_lots_b,
                'spread_units': sizing.spread_units(
                    pair.clip_lots_b, (pair.meta_b or {}).get('contract_size')),
                'market': md,
                'short_spread': (md or {}).get('short_spread'),
                'long_spread': (md or {}).get('long_spread'),
                # What the CARRY says this spread should be, from the
                # two things only the trader knows: the futures leg's
                # expiry and the swap per day at their broker.
                # Where to get out: break-even (the entry price plus
                # commission, both legs, both ends) and break-even plus
                # a target on the margin the spread ties up. A price on
                # the screen — nothing is sent to the broker.
                'exit': row_exit,
                'fair': self.fair_spread(pair, md),
                'errors': self.errors.get(key) or [],
                # The rows the ladder draws come from HERE, so the Work
                # column, the click that places an order and the price
                # that names it cannot disagree about where a level is.
                'rows': ladder_rows(pair, md, self.book, sizes=sizes,
                                    anchor=self._anchor_for(key, pair, md),
                                    frozen=bool(self._ladder_locked
                                                .get(key))),
                # What the window is built around, so the screen can say
                # how far the market has drifted from it.
                'ladder_anchor': self._ladder_anchor.get(key),
                # Published so a browser that reloaded, or a second one
                # on another screen, shows the tick that matches what
                # the engine is actually doing.
                'ladder_locked': bool(self._ladder_locked.get(key)),
                'depth_published': sizes['published'],
                'orders': [o.to_dict() for o in self.book.orders(key)],
                # Orders that STOPPED working recently, with the reason.
                # A rejected pending used to vanish from the ladder in
                # the same instant the click was accepted: the trader
                # saw a green toast and then an empty Work column, with
                # the broker's refusal nowhere on the screen.
                'dead_orders': [
                    o.to_dict() for o in self.book.orders(key,
                                                          working_only=False)
                    if not o.is_working and o.reason
                    and self.clock() - o.created_at < DEAD_ORDER_MEMORY_SEC],
                'quotes': self.quoter.snapshot(key),
                'quoting_leg': pair.quoting_leg,
                'leg_a_width': (pair.meta_a or {}).get('width'),
                'leg_b_width': (pair.meta_b or {}).get('width'),
                'working_buys': buys, 'working_sells': sells,
                # What is actually RESTING at the broker for this pair,
                # beside what our book thinks is working. They should
                # agree; when they do not, the ladder says so rather
                # than showing our own number twice.
                'broker_pendings': sum(
                    1 for quote in self.quoter.snapshot(key)
                    if quote.get('ticket')),
                'positions': positions,
                'net_position': net, 'avg_entry': avg_entry,
                'open_pnl': open_pnl if positions else None,
                'last_print': self.book.last_print(key),
            }
        return {
            'at': self.clock(),
            # What a click does, and how fast it is drained — the UI
            # arms itself from the ENGINE's answer, never from its own
            # idea of what the trader last selected.
            'confirm_market_clicks': bool(
                self.config.get('CONFIRM_MARKET_CLICKS', False)),
            'row_height_px': self.config.get('ROW_HEIGHT_PX', 17),
            # Which column the screen should treat as a buy. UI
            # only: the side reaches the engine already decided.
            'click_convention': self.config.get('CLICK_CONVENTION', 'TT'),
            #: How often the ladder re-centres on the mid, and whether a
            #: click away from the touch rests. Both are read from the
            #: ENGINE, never from the screen's own idea of them.
            'recentre_sec': self.config.get('RECENTRE_SEC', 5.0),
            'click_away_rests': bool(self.config.get('CLICK_AWAY_RESTS',
                                                     True)),
            'command_poll_sec': self.config.get('COMMAND_POLL_SEC', 0.02),
            # Published so a slow ladder can be blamed on the right
            # process instead of argued about (spec §9).
            'loop_interval_sec': self._loop_interval,
            'poll_target_sec': self.config.get('POLL_INTERVAL_SEC'),
            'accounts': {name: self.account_info(name) for name in self.legs},
            # Named, not omitted: an absent account looked exactly like
            # a quiet one on the screen.
            'dark_accounts': self.dark_accounts(),
            'reconciler': self.reconciler.snapshot(),
            'broker_clock': self.broker_clock(),
            'account_health': self.account_health(),
            'recovery': dict(self.recovery),
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

        The replace is RETRIED, because on Windows it fails outright
        while any other process has the destination open — and the web
        process opens it several times a second. Unretried, that
        collision failed the whole poll and froze the screen with the
        market moving (WinError 5).
        """
        with self.lock:
            payload = self.snapshot()
        tmp = self.status_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, default=str)
        atomicfile.replace(tmp, self.status_path)

    def serve_commands(self):
        """Drain clicks on their OWN thread, far faster than the poll.

        A click that waits for the next poll waits up to a whole poll
        interval before the order is even sent — 300ms of nothing, on a
        product whose entire promise is that one click is one order. The
        prices it acts on are the ones already published; nothing here
        needs a fresh poll to run.
        """
        every = float(self.config.get('COMMAND_POLL_SEC', 0.02))
        while not self._stop.is_set():
            try:
                if self.commands is not None:
                    self.commands.drain()
            except Exception as e:                  # never die on a command
                logging.exception("command drain failed: %s", e)
            self.sleep(every)

    def run(self):
        interval = float(self.config.get('POLL_INTERVAL_SEC', 0.3))
        self.start()
        while not self._stop.is_set():
            began = self.clock()
            try:
                self.retry_dark_legs()
            except Exception as e:               # never die on a retry
                logging.exception("leg retry failed: %s", e)
            try:
                if self.commands is not None:
                    # Also here, so a single-threaded run (the tests, and
                    # anything that does not start the command thread)
                    # still executes clicks.
                    self.commands.drain()
                self.poll_once()
                self.publish()
            except Exception as e:                     # never die on one poll
                logging.exception("poll failed: %s", e)
            self.run_session_cutoff()
            self.reconcile_if_due()
            self.journal_if_due()
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
                # "In profit" is NET P&L, less THIS ladder's commission —
                # read on any other basis it flattens trades that are not
                # actually in profit.
                _gross, net_pnl, _closing = mark_position(
                    position, md, pair.exit_settings(self.config.settings))
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
                self.remember(position)
                events.append({'pair': key, 'action': 'overnight_close',
                               'position': position.position_id,
                               'mode': pair.overnight.value,
                               'net_pnl': net_pnl, 'ok': result['ok']})
        self.session_events.extend(events)
        return events

    def journal_if_due(self):
        """Write what the BROKER says happened into the journal.

        Read from MT5's own deal history rather than from our intentions,
        so it also carries the trader's manual terminal clicks — `is_ours`
        says which is which. Idempotent by deal id, and on its own slow
        clock: a history query is not something to do three times a
        second.
        """
        if self.store is None:
            return None
        every = float(self.config.get('JOURNAL_INTERVAL_SEC', 10.0) or 0.0)
        if every <= 0:
            return None
        now = self.clock()
        if self._last_journal is not None and now - self._last_journal < every:
            return None
        self._last_journal = now
        written = 0
        for name, leg in self.legs.items():
            try:
                rows = leg.order_log(
                    int(self.config.get('JOURNAL_HOURS', 24)))
            except Exception as e:
                logging.error('journal: %s would not answer: %s', name, e)
                continue
            if rows is None:
                # None is UNKNOWN, not "nothing traded".
                logging.debug('journal: %s could not be read this pass', name)
                continue
            written += self.store.record_fills(name, rows, self.resolve_leg)
        return written

    def resolve_leg(self, account, symbol):
        """(pair key, 'A'/'B') for a fill, where it belongs to a pair.

        A fill we cannot map is still journalled: it happened on the
        account, and a journal that drops what it does not recognise is
        not an audit trail.
        """
        for key, pair in self.config.pairs.items():
            if pair.account_a == account and pair.symbol_a == symbol:
                return key, 'A'
            if pair.account_b == account and pair.symbol_b == symbol:
                return key, 'B'
        return None, None

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
            report = self.reconciler.run()
            # A force-cleared ghost is a state change, and the database
            # must not hand it back at the next restart.
            if report and (report['ghosts'] or report['closed']):
                self.remember_all()
            if self.store is not None and report and (
                    report['orphans'] or report['ghosts'] or report['closed']):
                self.store.event('reconcile', **{
                    'orphans': len(report['orphans']),
                    'ghosts': len(report['ghosts']),
                    'closed': report['closed'],
                    'unknown_accounts': report['unknown_accounts']})
            return report
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
        # What the contract itself says it expires on, so a blank
        # expiry field can fall back to MT5 rather than to a prompt.
        'expiry': report.get('expiry'),
        # The carry inputs. `swap_mode` travels with them ALWAYS: the
        # same -4.5 is points on one symbol, account currency on
        # another and percent a year on a third, and a swap without its
        # mode is not a smaller number, it is an unusable one.
        'swap_long': report.get('swap_long'),
        'swap_short': report.get('swap_short'),
        'swap_mode': report.get('swap_mode'),
        'swap_rollover3days': report.get('swap_rollover3days'),
    }


def _badge(md, stale, jumped):
    """The feed badge: OK / oldest leg 2.3s / desynced."""
    ages = [md.get('leg_a_quote_age_sec'), md.get('leg_b_quote_age_sec')]
    ages = [a for a in ages if a is not None]
    if jumped:
        return 'desynced'
    if stale:
        # WITH the number. "stale" alone says nothing about whether the
        # feed died or the market is simply quiet, and those want
        # different answers: one is a terminal to re-log in, the other
        # is a limit to raise.
        return f'stale {max(ages):.0f}s' if ages else 'stale'
    if not ages:
        return 'warming up'
    return f'OK (oldest leg {max(ages):.1f}s)'


#: A ladder that had to grow to reach a level stops somewhere: past
#: this many rows the increment is simply wrong for the pair, and
#: drawing 10,000 of them helps nobody.
MAX_LADDER_ROWS = 400
#: How long a DEAD order keeps its place on the screen. A rejection has
#: to outlive the poll that discovered it, or the trader sees a green
#: toast, an empty Work column, and no reason anywhere.
DEAD_ORDER_MEMORY_SEC = 90.0


def _same_login(rows):
    """Which MT5 logins are being reported by more than one account."""
    seen = {}
    for name, row in rows.items():
        login = row.get('login')
        if login:
            seen.setdefault(str(login), []).append(name)
    return {login: names for login, names in seen.items() if len(names) > 1}


def ladder_anchor(previous, spread, increment, rows, drift=0.34):
    """The price the ladder's window is built around, held STILL.

    The window used to be rebuilt around the live mid on every poll —
    three times a second. One increment of movement re-centred it, so
    the row under the trader's cursor became a DIFFERENT PRICE without
    the cursor moving: the ladder crawled, and a click landed on
    whatever had slid under it.

    The anchor therefore only moves when the mid has drifted past a
    band inside the window (a third of a half-window by default), or
    when there is no anchor yet. Between those the price at every row
    is exactly where it was, which is what makes the ladder clickable.
    """
    centre = round(spread / increment) * increment
    if previous is None:
        return centre
    if abs(spread - previous) > max(1.0, rows * drift) * increment:
        return centre
    return previous


def ladder_rows(pair, md, book, rows=None, sizes=None, anchor=None,
                frozen=False):
    """The rows the ladder draws: the anchor +/- N increments.

    Generated here rather than in the browser so the ladder cannot
    disagree with the engine about where a level is — the Work column,
    the click that places an order and the price that names it all come
    off this one list.

    The window is widened to cover both executable touches and every
    working order on the pair. A level the ladder cannot show is one the
    trader can neither see nor pull, and on a wide book the touches can
    sit well outside a window centred on the mid.

    `anchor` is the price the window is built around (see
    `ladder_anchor`). It is passed in rather than recomputed from the
    mid, because a window that follows every tick moves the prices out
    from under the cursor.
    """
    increment = pair.effective_increment()
    if not md or not increment:
        return []
    rows = int(rows or pair.rows)
    centre = (round(md['spread'] / increment) * increment if anchor is None
              else anchor)
    low = centre - rows * increment
    high = centre + rows * increment

    # The two touches move with every tick, so widening the window to
    # reach them ADDS AND REMOVES ROWS AT THE ENDS as the market walks
    # — and a row appearing above shifts every price below it down by
    # one. That is the second way a ladder crawls, and holding the
    # anchor still does nothing about it. A frozen ladder therefore
    # stops following the touches.
    #
    # Working orders keep their claim either way: their levels are
    # FIXED once placed, so they widen the window once and never
    # again, and a resting order the trader cannot see is one they
    # cannot pull.
    marks = [] if frozen else [md.get('short_spread'), md.get('long_spread')]
    marks += [order.level for order in book.orders(pair.key)]
    marks = [m for m in marks if m is not None]
    if marks:
        low = min([low] + marks)
        high = max([high] + marks)

    steps = int(round((high - low) / increment))
    if steps + 1 > MAX_LADDER_ROWS:
        # Keep the market itself; the far-away level is unreachable at
        # this increment and the ladder says so by not pretending.
        low = centre - (MAX_LADDER_ROWS // 2) * increment
        steps = MAX_LADDER_ROWS - 1

    sizes = sizes or {}
    out = []
    for step in range(steps, -1, -1):
        level = round(low + step * increment, 10)
        buys, sells = book.working_at(pair.key, level)
        out.append({
            'level': level,
            'work_buy': buys or None,
            'work_sell': sells or None,
            # The inside market: the two levels the black rule sits
            # between (spec §3.3).
            'is_best_bid': _at(level, md['short_spread'], increment),
            'is_best_ask': _at(level, md['long_spread'], increment),
            # The MID of the book — the row the ladder centres on, and
            # the one carrying the heavy rule. On a wide spread the two
            # touches can be many rows apart, and "where the market is"
            # is then a question the inside rule alone cannot answer.
            'is_mid': _at(level, md.get('spread'), increment),
            # What the two ORDER BOOKS can actually do here, in
            # spreads. None where a broker publishes no depth — which
            # is not the same as none available, and must not look it.
            'ask_size': depth_book.at(sizes.get('buy'), level, increment),
            'bid_size': depth_book.at(sizes.get('sell'), level, increment),
        })
    return out


def _at(level, price, increment):
    """Is this row the one that price falls in?"""
    if price is None:
        return False
    return abs(level - round(price / increment) * increment) < increment / 2


