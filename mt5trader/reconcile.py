"""Assume you will lose track. Then find out, and say so.

Two ways our book and the broker's disagree:

- **Orphans** — a position at the broker that is not in our book. It is
  real money, unhedged as far as we know, and nobody is watching it.
- **Ghosts** — a position in our book that is not at the broker. It was
  closed by hand, stopped out, or never really existed.

Both are settled on THREE strikes, not one: a single poll can catch the
broker mid-fill, and acting on that is how a healthy position gets
closed for being briefly invisible.

Three rules that cost money in the system this is ported from:

1. **P&L on an orphan close multiplies by CONTRACT SIZE.** That was
   wrong for months and booked closes at 1% of what they cost — $0.81
   where the answer was $81.10. An unknown symbol gets 1.0 and SAYS SO.
2. **A close that did not go through leaves the position OPEN and
   ACTIVE.** Setting an ERROR/CLOSING status removes it from every
   active lookup — the ladder, the monitor, and this module's own
   known-ticket set — so the money sits at the broker while the UI
   reads flat.
3. **None is not empty.** A leg that could not be read is UNKNOWN, and
   an unknown account produces no orphans and no ghosts. Treating it as
   flat would turn every position we hold into a ghost and clear the
   book while the money is still out there.
"""

import logging
import time

from .models import MAGIC_NUMBER, OrderSide


class Reconciler:
    def __init__(self, config, legs, book, executor, clock=time.time):
        self.config = config
        self.legs = legs
        self.book = book
        self.executor = executor
        self.clock = clock
        self.orphan_strikes = {}       # (account, ticket) -> count
        self.ghost_strikes = {}        # position_id -> count
        self.close_failures = {}       # (account, ticket) -> failed closes
        #: What we closed that we never opened, with what it cost. The
        #: operator can read this; it is not a log line.
        self.untracked_closes = []
        #: Tickets we have given up on, so the broker is not hammered.
        self.escalated = set()
        self.last_run = None
        self.unknown_accounts = []

    #: Strikes before acting. One poll can catch the broker mid-fill.
    STRIKES = 3

    def run(self):
        """One pass. Returns what it found and what it did."""
        self.last_run = self.clock()
        strikes_needed = self.STRIKES
        attempts = int(self.config.get('CLOSE_ATTEMPTS', 3))

        broker = {}
        self.unknown_accounts = []
        for name, leg in self.legs.items():
            positions = leg.positions()
            if positions is None:
                # UNKNOWN, not flat.
                self.unknown_accounts.append(name)
                logging.warning(
                    "reconcile: account '%s' could not be read — skipped, "
                    "not treated as flat", name)
                continue
            broker[name] = {str(p['ticket']): p for p in positions}

        known = self.known_tickets()
        report = {'at': self.last_run, 'orphans': [], 'ghosts': [],
                  'closed': [], 'escalated': [],
                  'unknown_accounts': list(self.unknown_accounts)}

        for account, positions in broker.items():
            for ticket, position in positions.items():
                if (account, ticket) in known:
                    continue
                key = (account, ticket)
                if key in self.escalated:
                    continue
                strikes = self.orphan_strikes.get(key, 0) + 1
                self.orphan_strikes[key] = strikes
                report['orphans'].append(
                    dict(position, account=account, strikes=strikes))
                if strikes < strikes_needed:
                    continue
                outcome = self._close_orphan(account, position, attempts)
                report['closed'].append(outcome)
                if outcome.get('escalated'):
                    report['escalated'].append(outcome)

        # Anything that came back is no longer striking out.
        for account, positions in broker.items():
            for account_ticket in list(self.orphan_strikes):
                if account_ticket[0] == account and \
                        account_ticket[1] not in positions:
                    self.orphan_strikes.pop(account_ticket, None)

        for position in self.book.positions():
            missing = self._missing_legs(position, broker)
            if not missing:
                self.ghost_strikes.pop(position.position_id, None)
                continue
            strikes = self.ghost_strikes.get(position.position_id, 0) + 1
            self.ghost_strikes[position.position_id] = strikes
            report['ghosts'].append({'position_id': position.position_id,
                                     'pair_key': position.pair_key,
                                     'missing': missing, 'strikes': strikes})
            if strikes >= strikes_needed:
                position.closed_at = self.clock()
                position.close_reason = (
                    f"force-cleared: {', '.join(missing)} not at the broker "
                    f"after {strikes} checks")
                logging.critical("%s: %s", position.pair_key,
                                 position.close_reason)
        return report

    def known_tickets(self):
        """Every (account, ticket) our OPEN positions hold.

        Open means ACTIVE. A position whose close failed is still open,
        so its tickets are still ours — dropping them here is what turns
        a failed close into an orphan we then close twice.
        """
        known = set()
        for position in self.book.positions():
            for fill in (position.leg_a, position.leg_b):
                if not fill:
                    continue
                for ticket in fill.position_tickets:
                    known.add((fill.account, str(ticket)))
        return known

    def _missing_legs(self, position, broker):
        missing = []
        for label, fill in (('leg A', position.leg_a), ('leg B', position.leg_b)):
            if not fill or not fill.position_tickets:
                continue
            if fill.account not in broker:
                continue                 # unknown account: no opinion
            at_broker = broker[fill.account]
            if not any(str(t) in at_broker for t in fill.position_tickets):
                missing.append(label)
        return missing

    def _close_orphan(self, account, position, attempts):
        """Close a position we never opened, and book what it cost."""
        leg = self.legs[account]
        ticket = str(position['ticket'])
        key = (account, ticket)
        entry_side = OrderSide(position['side'])
        before = leg.tick(position['symbol']) or {}
        result = leg.close_ticket(position['symbol'], int(ticket),
                                  position['volume'], entry_side.value,
                                  comment='RECONCILE')
        if not result.get('ok'):
            failures = self.close_failures.get(key, 0) + 1
            self.close_failures[key] = failures
            if failures >= attempts:
                # Escalate ONCE, then stop hammering the broker.
                self.escalated.add(key)
                logging.critical(
                    "CLOSE IT BY HAND: %s ticket %s on '%s' (%s %s lots) "
                    "would not close after %d attempts: %s",
                    position['symbol'], ticket, account, position['side'],
                    position['volume'], failures, result.get('error'))
                return {'account': account, 'ticket': ticket, 'ok': False,
                        'escalated': True, 'attempts': failures,
                        'error': result.get('error'),
                        'say': 'CLOSE IT BY HAND'}
            return {'account': account, 'ticket': ticket, 'ok': False,
                    'attempts': failures, 'error': result.get('error')}

        contract_size, assumed = self._contract_size(account,
                                                     position['symbol'])
        close_price = result.get('price') or (before.get('bid'))
        pnl = None
        if close_price is not None and position.get('price_open') is not None:
            move = close_price - position['price_open']
            if entry_side is OrderSide.SELL:
                move = -move
            # x CONTRACT SIZE. Without it these were booked at 1% of what
            # they really cost.
            pnl = move * position['volume'] * contract_size
        entry = {
            'at': self.clock(), 'account': account, 'ticket': ticket,
            'symbol': position['symbol'], 'side': position['side'],
            'volume': position['volume'], 'price_open': position['price_open'],
            'price_close': close_price, 'contract_size': contract_size,
            'contract_size_assumed': assumed, 'pnl': pnl, 'ok': True,
        }
        if assumed:
            entry['note'] = (f"{position['symbol']} is not a configured leg, "
                             f"so its contract size is ASSUMED to be 1.0 — "
                             f"this P&L is a lower bound, not a measurement")
            logging.warning("reconcile: %s", entry['note'])
        self.untracked_closes.append(entry)
        self.orphan_strikes.pop(key, None)
        self.close_failures.pop(key, None)
        logging.critical(
            "reconcile: closed ORPHAN %s ticket %s on '%s' — %s %s lots, "
            "P&L %s", position['symbol'], ticket, account, position['side'],
            position['volume'],
            f"${pnl:,.2f}" if pnl is not None else 'unmeasured')
        return entry

    def _contract_size(self, account, symbol):
        """(contract size, assumed?) — from the configured pairs' MT5
        metadata, never guessed silently."""
        for pair in self.config.pairs.values():
            if pair.account_a == account and pair.symbol_a == symbol:
                size = (pair.meta_a or {}).get('contract_size')
                if size:
                    return float(size), False
            if pair.account_b == account and pair.symbol_b == symbol:
                size = (pair.meta_b or {}).get('contract_size')
                if size:
                    return float(size), False
        return 1.0, True

    def snapshot(self):
        """What the positions monitor shows for the reconciler (spec §16)."""
        return {
            'last_run': self.last_run,
            'magic': MAGIC_NUMBER,
            'orphan_strikes': {f'{a}:{t}': n
                               for (a, t), n in self.orphan_strikes.items()},
            'ghost_strikes': dict(self.ghost_strikes),
            'untracked_closes': list(self.untracked_closes),
            'escalated': [f'{a}:{t}' for a, t in self.escalated],
            'unknown_accounts': list(self.unknown_accounts),
        }
