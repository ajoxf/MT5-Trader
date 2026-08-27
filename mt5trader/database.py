"""The local database: crash-safe state, and the trade journal.

Two jobs, and they are different in kind:

- **State.** Open positions live here as well as in memory, so a restart
  recovers what is actually on at the broker. Without it the book comes
  back empty, the reconciler reads every real position as an orphan,
  and sixty seconds later it closes them. That is the machinery working
  correctly on a book that lied to it.
- **The journal.** Every FILL the broker reports, ours and the trader's
  own terminal clicks alike, keyed by MT5's deal id. It is the record
  that survives us: an audit trail we did not write from our own
  intentions, but read back from what actually happened.

Mechanics that were paid for in the system this is ported from:

- **WAL and a 30-second busy timeout, through ONE `_connect()`.** The
  web process reads this file while the coordinator writes it, and the
  default raises `database is locked` immediately — which once stopped
  an exit loop for 30 seconds with a live position open.
- **A deal is written once**, by `INSERT OR REPLACE` on the deal id.
  Deal history is re-read on every pass and the same fill arrives many
  times; a journal that grows a row per read is not a journal.
- **Broker time is stored beside our own.** MT5 stamps deals with the
  SERVER's wall clock encoded as an epoch, and reading that as an
  ordinary timestamp puts every row hours away from the same trade in
  MT5's own History — which is enough to make the two tables look like
  different accounts.
"""

import json
import logging
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    position_id     TEXT PRIMARY KEY,
    pair_key        TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        REAL NOT NULL,
    entry_spread    REAL,
    exit_spread     REAL,
    spread_units    REAL,
    order_type      TEXT,
    opened_at       REAL,
    closed_at       REAL,
    close_reason    TEXT,
    realized_pnl    REAL,
    entry_slippage  REAL,
    exit_slippage   REAL,
    click_to_on_ms  REAL,
    leg_a           TEXT,       -- JSON: account, symbol, side, volume,
    leg_b           TEXT        --       price, tickets, contract size
);

CREATE INDEX IF NOT EXISTS positions_open
    ON positions (closed_at, pair_key);

-- The journal. One row per BROKER deal, keyed by the deal id AND the
-- account: deal ids are unique per broker, not across them, so two
-- accounts can each hold a deal #500001. Keyed on the id alone, one
-- would silently overwrite the other and the journal would be missing
-- a leg of every trade.
CREATE TABLE IF NOT EXISTS fills (
    deal_id         TEXT NOT NULL,
    account         TEXT NOT NULL,
    order_id        TEXT,
    position_ticket TEXT,
    symbol          TEXT,
    side            TEXT,        -- buy / sell
    entry           TEXT,        -- open / close
    order_type      TEXT,        -- market / buy limit / ...
    volume          REAL,
    price           REAL,
    commission      REAL,
    swap            REAL,
    profit          REAL,
    fee             REAL,
    broker_time_ms  INTEGER,     -- the SERVER's clock, as MT5 stamps it
    server_offset_s INTEGER,     -- ...and how far that is from UTC
    seen_at         REAL,        -- our own clock, when we first read it
    magic           INTEGER,
    is_ours         INTEGER,     -- our magic, or the trader's own click
    comment         TEXT,
    pair_key        TEXT,        -- resolved from account+symbol
    leg             TEXT,        -- 'A' or 'B' on that pair
    PRIMARY KEY (account, deal_id)
);

CREATE INDEX IF NOT EXISTS fills_time ON fills (broker_time_ms);
CREATE INDEX IF NOT EXISTS fills_pair ON fills (pair_key, broker_time_ms);

-- Everything else worth being able to answer "what happened at 14:32?"
-- with: refusals, sweeps, reconciler decisions, session cutoffs.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    at              REAL NOT NULL,
    kind            TEXT NOT NULL,
    pair_key        TEXT,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS events_at ON events (at);
"""


class Store:
    """One SQLite file, opened the same way everywhere."""

    def __init__(self, path='mt5trader.db', clock=time.time):
        self.path = path
        self.clock = clock
        self._ensure()

    def _connect(self):
        """The ONE way this database is opened.

        WAL so a reader never blocks the writer, and a 30-second busy
        timeout so the one that does wait, waits — rather than raising
        `database is locked` while a position is open.
        """
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA busy_timeout=30000')
        connection.execute('PRAGMA synchronous=NORMAL')
        return connection

    def _ensure(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    # -- positions: the state a restart recovers ---------------------------

    def save_position(self, position):
        """Write a position, open or closed. Called on every change.

        Cheap enough to do on every change and far cheaper than the
        alternative, which is a restart that cannot tell a live position
        from an orphan.
        """
        row = position.to_dict()
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO positions
                   (position_id, pair_key, side, quantity, entry_spread,
                    exit_spread, spread_units, order_type, opened_at,
                    closed_at, close_reason, realized_pnl, entry_slippage,
                    exit_slippage, click_to_on_ms, leg_a, leg_b)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row['position_id'], row['pair_key'], row['side'],
                 row['quantity'], row['entry_spread'], row['exit_spread'],
                 row['spread_units'], row['order_type'], row['opened_at'],
                 row['closed_at'], row['close_reason'], row['realized_pnl'],
                 row['entry_slippage'], row['exit_slippage'],
                 row['click_to_on_ms'], json.dumps(row['leg_a']),
                 json.dumps(row['leg_b'])))
        return position

    def open_positions(self):
        """Every position that was open when we last wrote it.

        A position whose close FAILED is still open, and comes back
        open: removing it from this list is how the money ends up at the
        broker with the screen reading flat.
        """
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM positions WHERE closed_at IS NULL '
                'ORDER BY opened_at').fetchall()
        return [_position_row(row) for row in rows]

    def closed_positions(self, limit=200):
        with self._connect() as connection:
            rows = connection.execute(
                'SELECT * FROM positions WHERE closed_at IS NOT NULL '
                'ORDER BY closed_at DESC LIMIT ?', (limit,)).fetchall()
        return [_position_row(row) for row in rows]

    def positions_between(self, start=None, end=None, pair_key=None):
        """Positions OPENED inside a window, open ones included.

        Anchored on `opened_at`, not on the close: a position belongs to
        the session it was PUT ON in, which is the session whose click
        the entry slippage was measured against. Anchoring on the close
        would move a trade carried overnight into the next day's report
        and credit its entry to a click nobody made that day.
        """
        where, params = [], []
        if start is not None:
            where.append('opened_at >= ?')
            params.append(start)
        if end is not None:
            where.append('opened_at <= ?')
            params.append(end)
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM positions {clause} ORDER BY opened_at',
                params).fetchall()
        return [_position_row(row) for row in rows]

    # -- the journal --------------------------------------------------------

    def record_fills(self, account, rows, resolve=None):
        """Write what the BROKER says happened. Idempotent by deal id.

        `rows` are `leg.order_log()` records — MT5's own deal history,
        which includes the trader's manual terminal clicks as well as
        ours. Both belong in the journal: a fill on the account is a
        fill on the account, and `is_ours` says which is which.

        `resolve(account, symbol)` maps a fill onto a configured pair
        and leg where it can; where it cannot, the row is still kept.
        """
        written = 0
        now = self.clock()
        with self._connect() as connection:
            for row in rows or ():
                if row.get('inst_type') != 'DEAL':
                    # Orders and pendings are not fills. They are in the
                    # events table and in MT5's own order history.
                    continue
                deal_id = str(row.get('deal_id') or '')
                if not deal_id:
                    continue
                pair_key, leg = (resolve(account, row.get('symbol'))
                                 if resolve else (None, None))
                connection.execute(
                    """INSERT OR REPLACE INTO fills
                       (deal_id, account, order_id, position_ticket, symbol,
                        side, entry, order_type, volume, price, commission,
                        swap, profit, fee, broker_time_ms, server_offset_s,
                        seen_at, magic, is_ours, comment, pair_key, leg)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                               COALESCE((SELECT seen_at FROM fills
                                         WHERE account = ? AND deal_id = ?),
                                        ?),
                               ?,?,?,?,?)""",
                    (deal_id, account, str(row.get('order_id') or ''),
                     str(row.get('position_id') or ''), row.get('symbol'),
                     row.get('side'), row.get('pos_side'),
                     row.get('order_type'), row.get('fill_qty'),
                     row.get('fill_price'), row.get('commission'),
                     row.get('swap'), row.get('pnl'), row.get('fee'),
                     row.get('filled_at'), row.get('server_offset_sec'),
                     account, deal_id, now,
                     row.get('magic'), 1 if row.get('is_bot') else 0,
                     row.get('comment'), pair_key, leg))
                written += 1
        return written

    def fills(self, pair_key=None, account=None, ours_only=False,
              since_ms=None, limit=500):
        """The journal, newest first."""
        where, params = [], []
        if pair_key:
            where.append('pair_key = ?')
            params.append(pair_key)
        if account:
            where.append('account = ?')
            params.append(account)
        if ours_only:
            where.append('is_ours = 1')
        if since_ms:
            where.append('broker_time_ms >= ?')
            params.append(since_ms)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM fills {clause} '
                f'ORDER BY broker_time_ms DESC, rowid DESC LIMIT ?',
                params).fetchall()
        return [dict(row) for row in rows]

    def fill_totals(self, pair_key=None):
        """What the journal adds up to — from the BROKER's numbers.

        This is the honest counterweight to our own marks: commission
        and swap are the broker's, and profit on a closing deal is what
        MT5 itself booked.
        """
        clause = 'WHERE pair_key = ?' if pair_key else ''
        params = (pair_key,) if pair_key else ()
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS fills,
                           COALESCE(SUM(volume), 0) AS volume,
                           COALESCE(SUM(commission), 0) AS commission,
                           COALESCE(SUM(swap), 0) AS swap,
                           COALESCE(SUM(profit), 0) AS profit
                    FROM fills {clause}""", params).fetchone()
        return dict(row)

    def fills_between(self, from_ms=None, to_ms=None, ours_only=True):
        """What the BROKER filled in a window, on the broker's stamps.

        The counterweight to the slippage report: the report is built
        from OUR positions, and this says how many deals the account
        actually saw over the same stretch. Two numbers that disagree
        mean fills the report is blind to — a manual click in the
        terminal, or a leg we never paired — and that is worth showing
        rather than reconciling away.
        """
        where, params = [], []
        if ours_only:
            where.append('is_ours = 1')
        if from_ms is not None:
            where.append('broker_time_ms >= ?')
            params.append(from_ms)
        if to_ms is not None:
            where.append('broker_time_ms <= ?')
            params.append(to_ms)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with self._connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS fills,
                           COALESCE(SUM(volume), 0) AS volume,
                           COALESCE(SUM(commission), 0) AS commission,
                           COALESCE(SUM(swap), 0) AS swap,
                           COALESCE(SUM(profit), 0) AS profit
                    FROM fills {clause}""", params).fetchone()
        return dict(row)

    # -- events -------------------------------------------------------------

    def event(self, kind, pair_key=None, **detail):
        """One line of the audit trail. Never a substitute for saying it
        on the screen — this is for answering questions afterwards."""
        with self._connect() as connection:
            connection.execute(
                'INSERT INTO events (at, kind, pair_key, detail) '
                'VALUES (?,?,?,?)',
                (self.clock(), kind, pair_key, json.dumps(detail,
                                                          default=str)))

    def events(self, kind=None, limit=200):
        clause = 'WHERE kind = ?' if kind else ''
        params = ([kind] if kind else []) + [limit]
        with self._connect() as connection:
            rows = connection.execute(
                f'SELECT * FROM events {clause} ORDER BY at DESC LIMIT ?',
                params).fetchall()
        return [dict(row, detail=json.loads(row['detail'] or '{}'))
                for row in rows]


def _position_row(row):
    """A stored row, as the plain dict the book rebuilds a position from."""
    data = dict(row)
    for leg in ('leg_a', 'leg_b'):
        try:
            data[leg] = json.loads(data[leg]) if data[leg] else None
        except ValueError:
            logging.error('position %s has an unreadable %s',
                          data.get('position_id'), leg)
            data[leg] = None
    return data
