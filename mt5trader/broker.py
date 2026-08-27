"""Broker session: the only module that touches the MetaTrader5 package.

HARD CONSTRAINT: the MetaTrader5 Python package holds ONE global
connection per process. Initializing a second session replaces the
first. True simultaneous streaming from two accounts therefore
requires one process per account plus a coordinator — which is why
this product runs one leg runner per account (see the spec, section 1).
This class makes the connection explicit (path/login/server from
config) so each such process can be pointed at its own terminal.

Ported from the stat-arb system, where every quirk in here was paid
for on a live account: 10015 invalid price, the filling-mode bitmask,
10027 AutoTrading disabled, deal history lagging positions_get, re-peg
by MODIFY rather than cancel-and-replace, and closes that target a
position TICKET because these accounts are hedging mode.
"""

import logging
import time
from datetime import datetime, timedelta

from . import mt5_errors
from .models import MAGIC_NUMBER, OrderSide

try:
    import MetaTrader5 as mt5
except ImportError:  # not available off-Windows; tests use FakeBroker
    mt5 = None


class OrderResult:
    """Outcome of a market order, decoupled from mt5 result objects."""

    def __init__(self, success, requested_price=None, executed_price=None,
                 ticket=None, error=None, volume=0.0):
        self.success = success
        self.requested_price = requested_price
        self.executed_price = executed_price
        self.ticket = ticket
        self.error = error
        self.volume = volume  # filled volume (IOC may partially fill)


class BrokerSession:
    """One MT5 terminal connection for one account."""

    def __init__(self, account):
        self.account = account
        self.connected = False

    def initialize(self):
        """Connect this process to one MT5 terminal.

        Attempts, in order (first success wins):
        1. path + credentials — launch/attach a SPECIFIC terminal
           installation (needed when two brokers run side by side);
        2. credentials only — attach to the terminal that is ALREADY
           OPEN and log into this account;
        3. bare attach — use whatever terminal is open and logged in.

        The fallbacks matter: launching terminal64.exe from Python is
        brittle (wrong path, portable mode, auto-login disabled),
        while attaching to a terminal the operator already opened is
        the pattern that works reliably in practice.
        """
        if mt5 is None:
            logging.error(
                "MetaTrader5 package not installed (Windows-only). "
                "Install it on the trading machine.")
            return False

        credentials = {}
        if self.account.login:
            credentials = {'login': self.account.login,
                           'password': self.account.password or "",
                           'server': self.account.server or ""}

        attempts = []
        if self.account.terminal_path:
            attempts.append(("terminal path + credentials",
                             dict(credentials,
                                  path=self.account.terminal_path)))
        if credentials:
            attempts.append(("running terminal + credentials",
                             dict(credentials)))
        attempts.append(("running terminal (already logged in)", {}))

        for label, kwargs in attempts:
            # Announce BEFORE the call, at INFO. mt5.initialize(path=)
            # launches a terminal and waits for it to log in, so it can
            # block for a long time or forever — and until it returns
            # there is nothing on screen at all. Live 2026-08-11 the
            # second leg runner logged "Configuration loaded" and then
            # went silent, while the console showed only a coordinator
            # restart loop with no hint of which leg was stuck or why.
            logging.info("[%s] connecting to MT5 via %s%s", self.account.name,
                         label,
                         (f" ({self.account.terminal_path})"
                          if kwargs.get('path') else ''))
            try:
                ok = mt5.initialize(**kwargs)
            except Exception as e:                      # bad path types etc
                logging.debug("MT5 initialize(%s) raised: %s", label, e)
                ok = False
            if not ok:
                # Release before trying the next form. A failed
                # initialize can still leave the library half-attached
                # to a terminal, and the next attempt then reports a
                # fault belonging to the previous one — which makes the
                # attempt list actively misleading rather than
                # progressively more forgiving.
                try:
                    mt5.shutdown()
                except Exception:
                    pass
            if ok:
                self.connected = True
                info = mt5.account_info()
                if info:
                    logging.info("Connected [%s] via %s: %s / %s (login %s)",
                                 self.account.name, label, info.server,
                                 info.name, info.login)
                    if self.account.login and info.login != self.account.login:
                        logging.warning(
                            "[%s] terminal is logged into %s but config "
                            "expects %s — check the account mapping",
                            self.account.name, info.login,
                            self.account.login)
                else:
                    logging.info("Connected [%s] via %s",
                                 self.account.name, label)
                return True
            logging.debug("MT5 initialize failed (%s) for '%s': %s",
                          label, self.account.name, mt5.last_error())

        # Decode it rather than printing the raw tuple. -6 in
        # particular is not always a typo: live 2026-08-11 the terminal
        # journal read "authorization ... failed (Invalid account)" for
        # a login that simply did not exist on that server, and the
        # generic "check login/server/password" sent the operator round
        # the settings for an hour.
        error = mt5.last_error()
        summary, fixes = mt5_errors.explain(error)
        logging.error("MT5 connection failed for account '%s': %s%s",
                      self.account.name, error,
                      f' — {summary}' if summary else '')
        for fix in (fixes or [
                'Open the MT5 terminal for this account and log in '
                '(then a blank terminal path is fine)',
                'Or set the correct path to terminal64.exe in Settings',
                'Or check login / server / password']):
            logging.error("    fix: %s", fix)
        return False

    def shutdown(self):
        if mt5 is not None:
            mt5.shutdown()
        self.connected = False

    def is_alive(self):
        return mt5 is not None and mt5.terminal_info() is not None

    def account_info(self):
        return mt5.account_info() if mt5 else None

    def symbol_info(self, symbol):
        return mt5.symbol_info(symbol) if mt5 else None

    def ensure_symbol(self, symbol):
        """Return symbol info, selecting it into Market Watch if hidden."""
        info = self.symbol_info(symbol)
        if info and not info.visible:
            mt5.symbol_select(symbol, True)
        return info

    def symbol_tick(self, symbol):
        return mt5.symbol_info_tick(symbol) if mt5 else None

    def find_symbols(self, pattern, limit=40):
        """Symbols on THIS broker whose name or description matches —
        brokers name the same instrument differently (XAUUSD, GOLD,
        XAUUSD.r), so the operator needs to search rather than guess."""
        if mt5 is None:
            return []
        needle = (pattern or '').strip().upper()
        found = []
        for info in (mt5.symbols_get() or ()):
            name = info.name.upper()
            description = (getattr(info, 'description', '') or '').upper()
            if needle and needle not in name and needle not in description:
                continue
            found.append({
                'symbol': info.name,
                'description': getattr(info, 'description', ''),
                'path': getattr(info, 'path', ''),
                'visible': bool(info.visible),
                'contract_size': getattr(info, 'trade_contract_size', None),
                'volume_min': getattr(info, 'volume_min', None),
                'volume_max': getattr(info, 'volume_max', None),
                'volume_step': getattr(info, 'volume_step', None),
                'currency': getattr(info, 'currency_profit', ''),
                'expiry': getattr(info, 'expiration_time', 0),
            })
            if len(found) >= limit:
                break
        return found

    def session_stats(self, symbol):
        """This symbol's own session O/H/L and volume, as the TERMINAL
        reports them.

        Read defensively with getattr: brokers fill in different subsets
        of these fields, and a missing one must come back as None rather
        than as a zero that would be drawn as a real high of 0.00.
        """
        if mt5 is None:
            return None
        info = self.ensure_symbol(symbol)
        if info is None:
            return None

        def value(*names):
            for name in names:
                found = getattr(info, name, None)
                if found not in (None, 0.0, 0):
                    return float(found)
            return None

        return {
            'symbol': symbol,
            'open': value('session_open'),
            'high': value('session_high', 'bidhigh'),
            'low': value('session_low', 'bidlow'),
            # Tick volume, which is what MT5 has for most instruments.
            'volume': value('session_volume', 'volume'),
        }

    def symbol_report(self, symbol):
        """Everything the connectivity checklist needs about one symbol
        on this account: does it exist, is it in Market Watch, is it
        priced, and what are the contract specs the sizing math and the
        hedge ratio depend on."""
        if mt5 is None:
            return {'symbol': symbol, 'found': False,
                    'error': 'MetaTrader5 package not installed'}
        info = self.ensure_symbol(symbol)
        if info is None:
            return {'symbol': symbol, 'found': False,
                    'error': f'{symbol} does not exist on this broker'}
        tick = mt5.symbol_info_tick(symbol)
        trade_mode = getattr(info, 'trade_mode', None)
        return {
            'symbol': symbol, 'found': True,
            'description': getattr(info, 'description', ''),
            'visible': bool(info.visible),
            'bid': tick.bid if tick else None,
            'ask': tick.ask if tick else None,
            'tick_time': int(getattr(tick, 'time', 0)) if tick else None,
            'digits': getattr(info, 'digits', None),
            'point': getattr(info, 'point', None),
            'tick_size': (getattr(info, 'trade_tick_size', 0)
                          or getattr(info, 'point', None)),
            'contract_size': getattr(info, 'trade_contract_size', None),
            # What MT5 says one tick of movement is WORTH on one lot.
            # tick_value / tick_size is the contract size the terminal
            # will actually compute profit from, whatever
            # trade_contract_size claims — so when a broker's spec
            # sheet and the terminal disagree, this settles it, because
            # this is what the money is calculated from.
            'tick_value': getattr(info, 'trade_tick_value', None),
            'volume_min': getattr(info, 'volume_min', None),
            'volume_max': getattr(info, 'volume_max', None),
            'volume_step': getattr(info, 'volume_step', None),
            'currency': getattr(info, 'currency_profit', ''),
            'filling_mode': getattr(info, 'filling_mode', None),
            'trade_mode': trade_mode,
            'trade_allowed': (trade_mode not in
                              (getattr(mt5, 'SYMBOL_TRADE_MODE_DISABLED', 0),
                               getattr(mt5, 'SYMBOL_TRADE_MODE_CLOSEONLY',
                                       -1))),
            'expiry': int(getattr(info, 'expiration_time', 0) or 0),
            'swap_long': getattr(info, 'swap_long', None),
            'swap_short': getattr(info, 'swap_short', None),
            # WHAT swap_long/short are denominated in. Without it the
            # numbers are unusable: the same "-4.5" is 4.5 points on one
            # symbol, 4.5 account-currency units on another and 4.5
            # percent a year on a third. Reading it as money regardless
            # is how the old carry term produced a basis nobody could
            # reconcile.
            'swap_mode': getattr(info, 'swap_mode', None),
            # Which weekday is charged triple for the weekend, so a
            # holding period can count nights rather than days.
            'swap_rollover3days': getattr(info, 'swap_rollover3days', None),
        }

    def verify_ticket(self, ticket, attempts=3, delay=0.4):
        """Ask MT5 what IT has for this ticket — the independent proof
        that an order really reached the broker rather than just
        returning success from order_send.

        Looks for the position (still open), then the deals it
        produced, then the order record. Deal history lags a fill by a
        moment, so a miss is retried before it is believed."""
        if mt5 is None:
            return {'ticket': ticket, 'confirmed': False,
                    'error': 'MetaTrader5 package not installed'}
        found = {'ticket': ticket, 'confirmed': False, 'deals': [],
                 'position_open': False}
        for attempt in range(attempts):
            try:
                positions = mt5.positions_get(ticket=int(ticket)) or ()
                for position in positions:
                    found.update({
                        'confirmed': True, 'position_open': True,
                        'symbol': position.symbol,
                        'volume': position.volume,
                        'price': position.price_open,
                        'time': int(position.time),
                        'magic': position.magic,
                        'comment': position.comment or '',
                        'source': 'open position'})

                deals = (mt5.history_deals_get(position=int(ticket))
                         or mt5.history_deals_get(ticket=int(ticket)) or ())
                for deal in deals:
                    found['deals'].append({
                        'deal_id': deal.ticket, 'order_id': deal.order,
                        'symbol': deal.symbol, 'volume': deal.volume,
                        'price': deal.price,
                        'commission': deal.commission,
                        'profit': deal.profit,
                        'time': int(deal.time),
                        'comment': deal.comment or ''})
                if deals:
                    last = found['deals'][-1]
                    found.update({'confirmed': True,
                                  'symbol': last['symbol'],
                                  'volume': last['volume'],
                                  'price': last['price'],
                                  'time': last['time'],
                                  'source': 'deal history'})

                if not found['confirmed']:
                    orders = (mt5.history_orders_get(ticket=int(ticket))
                              or ())
                    for order in orders:
                        found.update({
                            'confirmed': True, 'symbol': order.symbol,
                            'volume': order.volume_initial,
                            'price': order.price_open,
                            'time': int(getattr(order, 'time_done',
                                                order.time_setup)),
                            'state': order.state,
                            'comment': order.comment or '',
                            'source': 'order history'})
            except Exception as e:
                found['error'] = str(e)
            if found['confirmed'] or attempt == attempts - 1:
                break
            time.sleep(delay)          # history lags a fill briefly
        if not found['confirmed']:
            found.setdefault('error', 'not found in MT5 positions, deals '
                                      'or order history')
        return found

    def terminal_report(self):
        """Terminal- and account-level facts the checklist reports:
        whether the terminal is attached, who is logged in, whether
        algo trading is switched on, and the account's margin mode."""
        if mt5 is None:
            return {'library': False, 'terminal': False,
                    'error': 'MetaTrader5 package not installed '
                             '(Windows only)'}
        report = {'library': True, 'terminal': False}
        terminal = mt5.terminal_info()
        if terminal is None:
            report['error'] = str(mt5.last_error())
            return report
        report.update({
            'terminal': True,
            'terminal_name': getattr(terminal, 'name', ''),
            'terminal_path': getattr(terminal, 'path', ''),
            'terminal_connected': bool(getattr(terminal, 'connected', False)),
            'algo_trading': bool(getattr(terminal, 'trade_allowed', False)),
            'ping_ms': (getattr(terminal, 'ping_last', 0) or 0) / 1000.0,
        })
        info = mt5.account_info()
        if info is None:
            report['logged_in'] = False
            return report
        margin_mode = getattr(info, 'margin_mode', None)
        report.update({
            'logged_in': True,
            'login': info.login, 'server': info.server,
            'name': getattr(info, 'name', ''),
            'currency': getattr(info, 'currency', ''),
            'leverage': getattr(info, 'leverage', None),
            'balance': getattr(info, 'balance', 0.0),
            # equity = balance + credit + floating P&L. Brokers often
            # fund a demo with CREDIT rather than balance, which makes
            # balance alone read as an empty account: live 2026-08-11,
            # balance 0.00 against equity 5,000, and on the account
            # before it balance -13.70 against equity 4,986.30.
            'credit': getattr(info, 'credit', 0.0),
            'equity': getattr(info, 'equity', 0.0),
            'margin_free': getattr(info, 'margin_free', 0.0),
            'trade_allowed': bool(getattr(info, 'trade_allowed', False)),
            'trade_expert': bool(getattr(info, 'trade_expert', False)),
            'margin_mode': margin_mode,
            'hedging': margin_mode == getattr(
                mt5, 'ACCOUNT_MARGIN_MODE_RETAIL_HEDGING', 2),
        })
        return report

    def server_time_offset_sec(self):
        """Seconds the BROKER's displayed clock runs ahead of UTC.

        MT5 stamps every deal and order with the server's WALL CLOCK
        encoded as a Unix epoch, and the History tab displays that same
        wall clock. Read one of those stamps as an ordinary timestamp —
        which is what the dashboard was doing — and you get the
        browser's local rendering of a number that was never in the
        browser's time zone. On a GMT+3 broker seen from a GMT+5:30
        box, every row in our order log sits 2.5 hours away
        from the same trade in MT5's History, which is enough on its
        own to make the two tables look like different accounts.

        Measured from the freshest tick we can see (a tick's `time` is
        stamped the same way), so it needs a symbol in Market Watch and
        returns None when it cannot be established — a guess here would
        be worse than an honest blank."""
        if mt5 is None:
            return None
        newest = None
        for name in self._time_probe_symbols():
            tick = mt5.symbol_info_tick(name)
            stamp = getattr(tick, 'time', None) if tick else None
            if stamp:
                newest = max(newest or 0, int(stamp))
        if not newest:
            return None
        return int(round(newest - time.time()))

    def _time_probe_symbols(self):
        """Symbols to read the server clock off: whatever is already in
        Market Watch. Cheap, and it needs no configuration."""
        try:
            return [s.name for s in (mt5.symbols_get() or ()) if s.visible]
        except Exception:
            return []

    def order_log(self, hours=24):
        """Everything this MT5 account did recently, normalised for the
        order log: filled deals (with fee/swap/profit), plus
        orders that never filled (cancelled/rejected) and anything
        still resting. Includes manual trades placed in the terminal —
        `is_bot` marks the ones this engine sent."""
        if mt5 is None:
            return []
        rows = []
        offset = self.server_time_offset_sec()
        try:
            # These bounds are matched against SERVER-clock stamps
            # while `datetime.now()` is this box's local clock. A
            # `now + 1 minute` ceiling therefore silently dropped the
            # most recent deals whenever the server clock ran ahead of
            # the box — the newest rows, which are exactly the ones an
            # operator is checking against MT5's History. The widest
            # real gap (UTC-12 to UTC+14, plus DST) is 27 hours, so pad
            # both ends by two days and trim afterwards on the rows'
            # own stamps. Over-fetching an audit log costs nothing;
            # missing a fill costs trust in the whole table.
            slack = timedelta(days=2)
            since = datetime.now() - timedelta(hours=hours) - slack
            now = datetime.now() + slack

            order_types = {
                getattr(mt5, name, -1): label for name, label in [
                    ('ORDER_TYPE_BUY', 'market buy'),
                    ('ORDER_TYPE_SELL', 'market sell'),
                    ('ORDER_TYPE_BUY_LIMIT', 'buy limit'),
                    ('ORDER_TYPE_SELL_LIMIT', 'sell limit'),
                    ('ORDER_TYPE_BUY_STOP', 'buy stop'),
                    ('ORDER_TYPE_SELL_STOP', 'sell stop')]}

            # A DEAL record does not carry the order type, so the log
            # used to print a literal "market/limit" on every filled
            # row — the one column an operator checks to confirm the
            # limit path actually rested rather than crossing. The
            # ORDER that produced the deal does know, and deal.order
            # points straight at it, so resolve it here rather than
            # showing both and meaning neither.
            history_orders = list(mt5.history_orders_get(since, now) or ())
            type_by_order = {
                str(o.ticket): order_types.get(o.type, str(o.type))
                for o in history_orders}

            deal_types = {0: 'buy', 1: 'sell'}
            for deal in (mt5.history_deals_get(since, now) or ()):
                if deal.type not in deal_types:
                    continue          # balance/credit entries, not trades
                order_id = str(deal.order or deal.ticket)
                rows.append({
                    'order_id': order_id,
                    'deal_id': str(deal.ticket),
                    'symbol': deal.symbol,
                    'inst_type': 'DEAL',
                    'side': deal_types[deal.type],
                    'pos_side': ('open' if deal.entry == mt5.DEAL_ENTRY_IN
                                 else 'close'),
                    # Unknown only when the originating order has aged
                    # out of the history window — never a guess.
                    'order_type': type_by_order.get(order_id, 'unknown'),
                    'quantity': deal.volume,
                    'fill_qty': deal.volume,
                    'fill_price': deal.price,
                    'fee': (deal.commission or 0.0) + (deal.swap or 0.0),
                    'fee_ccy': '',
                    'pnl': deal.profit or 0.0,
                    'state': 'filled',
                    'filled_at': int(deal.time) * 1000,
                    'position_id': deal.position_id,
                    'is_bot': deal.magic == MAGIC_NUMBER,
                    'comment': deal.comment or '',
                })

            states = {
                getattr(mt5, name, -1): label for name, label in [
                    ('ORDER_STATE_STARTED', 'started'),
                    ('ORDER_STATE_PLACED', 'placed'),
                    ('ORDER_STATE_CANCELED', 'cancelled'),
                    ('ORDER_STATE_PARTIAL', 'partial'),
                    ('ORDER_STATE_FILLED', 'filled'),
                    ('ORDER_STATE_REJECTED', 'rejected'),
                    ('ORDER_STATE_EXPIRED', 'expired')]}

            # Orders that never produced a deal still matter — a
            # rejection or a cancel is exactly what you go looking for.
            for order in history_orders:
                state = states.get(order.state, str(order.state))
                if state in ('filled', 'partial'):
                    continue          # already covered by its deal
                rows.append({
                    'order_id': str(order.ticket), 'deal_id': '',
                    'symbol': order.symbol, 'inst_type': 'ORDER',
                    'side': ('buy' if 'buy' in
                             order_types.get(order.type, '') else 'sell'),
                    'pos_side': '-',
                    'order_type': order_types.get(order.type,
                                                  str(order.type)),
                    'quantity': order.volume_initial,
                    'fill_qty': 0.0,
                    'fill_price': order.price_open or 0.0,
                    'fee': 0.0, 'fee_ccy': '', 'pnl': 0.0,
                    'state': state,
                    'filled_at': int(getattr(order, 'time_done',
                                             order.time_setup)) * 1000,
                    'position_id': order.position_id,
                    'is_bot': order.magic == MAGIC_NUMBER,
                    'comment': order.comment or '',
                })

            for order in (mt5.orders_get() or ()):
                rows.append({
                    'order_id': str(order.ticket), 'deal_id': '',
                    'symbol': order.symbol, 'inst_type': 'PENDING',
                    'side': ('buy' if 'buy' in
                             order_types.get(order.type, '') else 'sell'),
                    'pos_side': '-',
                    'order_type': order_types.get(order.type,
                                                  str(order.type)),
                    'quantity': order.volume_initial,
                    'fill_qty': (order.volume_initial
                                 - order.volume_current),
                    'fill_price': order.price_open or 0.0,
                    'fee': 0.0, 'fee_ccy': '', 'pnl': 0.0,
                    'state': 'working',
                    'filled_at': int(order.time_setup) * 1000,
                    'position_id': order.position_id,
                    'is_bot': order.magic == MAGIC_NUMBER,
                    'comment': order.comment or '',
                })
        except Exception as e:
            logging.error("order_log failed: %s", e)

        # The padded window above over-fetches on purpose. Trim back to
        # what was asked for using each row's OWN stamp against the
        # server clock — the same clock the stamps are in. Without a
        # measured offset we keep everything rather than cut blind.
        if offset is not None and hours:
            cutoff_ms = int((time.time() + offset - hours * 3600) * 1000)
            rows = [r for r in rows
                    if r['state'] == 'working'
                    or (r.get('filled_at') or 0) >= cutoff_ms]
        # Every row carries the offset so the dashboard can render the
        # broker's own clock beside MT5's History instead of the
        # browser's, which is what made the two tables disagree.
        for row in rows:
            row['server_offset_sec'] = offset
        return rows

    def positions_by_magic(self, symbol=None):
        """Open positions created by THIS system (magic-scoped) —
        never touches manual or third-party positions."""
        if mt5 is None:
            return []
        raw = (mt5.positions_get(symbol=symbol) if symbol
               else mt5.positions_get()) or ()
        out = []
        for p in raw:
            if p.magic != MAGIC_NUMBER:
                continue
            out.append({
                'ticket': p.ticket,
                'symbol': p.symbol,
                'side': ('BUY' if p.type == mt5.POSITION_TYPE_BUY
                         else 'SELL'),
                'volume': p.volume,
                'price_open': p.price_open,
            })
        return out

    def account_is_hedging(self):
        """True when the account holds one position per order (hedging
        mode) rather than netting per symbol."""
        if mt5 is None:
            return False
        info = mt5.account_info()
        return bool(info) and info.margin_mode == \
            mt5.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    def symbol_filling_modes(self, symbol):
        """Which type_filling values this symbol allows (broker-dependent)."""
        info = self.symbol_info(symbol)
        if not info:
            return []
        mask = getattr(info, 'filling_mode', 0)
        modes = []
        if mask & 1:
            modes.append('FOK')
        if mask & 2:
            modes.append('IOC')
        modes.append('RETURN')  # always available for pending orders
        return modes

    def _market_filling_modes(self, symbol):
        """Filling modes to try for a MARKET order, best first.

        symbol_info.filling_mode is a bitmask of what the broker allows
        (FOK=1, IOC=2). Hardcoding IOC here used to make every close
        fail with 10030 'Unsupported filling mode' on brokers that only
        allow FOK — the engine could open but never exit."""
        info = self.symbol_info(symbol)
        mask = getattr(info, 'filling_mode', 0) if info else 0
        modes = []
        if mask & 2:
            modes.append(mt5.ORDER_FILLING_IOC)   # allows partial fills
        if mask & 1:
            modes.append(mt5.ORDER_FILLING_FOK)
        if not modes:
            # Nothing declared: try all three rather than guess wrong.
            modes = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK,
                     mt5.ORDER_FILLING_RETURN]
        elif mt5.ORDER_FILLING_RETURN not in modes:
            modes.append(mt5.ORDER_FILLING_RETURN)
        return modes

    def _send_market(self, request, symbol):
        """order_send, retrying with another filling mode if the broker
        rejects the one we chose. Returns (result, last_mode)."""
        result = None
        for mode in self._market_filling_modes(symbol):
            request["type_filling"] = mode
            result = mt5.order_send(request)
            if result is None:
                continue
            if result.retcode != 10030:        # not a filling-mode problem
                return result, mode
            logging.debug("%s rejected filling mode %s (10030) — retrying",
                          symbol, mode)
        return result, None

    def _filling_hint(self, symbol):
        info = self.symbol_info(symbol)
        mask = getattr(info, 'filling_mode', 0) if info else 0
        allowed = [name for bit, name in ((1, 'FOK'), (2, 'IOC')) if mask & bit]
        return (f"broker allows {'/'.join(allowed)} for {symbol}"
                if allowed else
                f"{symbol} declares no filling mode")

    def _pending_filling_mode(self, symbol):
        """Pick a filling mode the broker actually accepts for pending
        orders. RETURN is the default, but some brokers only allow
        FOK/IOC (flags in symbol_info.filling_mode) and reject RETURN
        with 'Unsupported filling mode' — live-tested 2026-06."""
        info = self.symbol_info(symbol)
        mask = getattr(info, 'filling_mode', 0) if info else 0
        if mask & 1:
            return mt5.ORDER_FILLING_FOK
        if mask & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def legal_limit_price(self, symbol, side, price):
        """The nearest price MT5 will actually ACCEPT for this limit.

        A pending price has to clear two separate constraints, and
        missing either one comes back as the same opaque 10015 Invalid
        price:

        1. It must land on a `trade_tick_size` boundary.
        2. It must sit at least `trade_stops_level` POINTS away from the
           market — BUY_LIMIT that far below the ask, SELL_LIMIT that
           far above the bid. Brokers set this per symbol.

        Only (1) was enforced. That was survivable on CFI's gold, where
        the stops level is 0, so a limit one tick inside the touch was
        legal. On their oil symbols it is not: live 2026-08-07, every
        BUY_SPOT LIMIT scenario failed `10015 - Invalid price` on
        USOIL_U6 while the identical code passed on XAUUSD_.

        Returns (price, note) — `note` is None when nothing had to move,
        otherwise it says what the broker's rule forced, so a limit that
        could not rest where it was asked to says so instead of looking
        like a clean fill at a price nobody chose.
        """
        info = self.symbol_info(symbol)
        tick_size = (getattr(info, 'trade_tick_size', 0)
                     or getattr(info, 'point', 0.01) or 0.01)

        def to_tick(value, up=False):
            steps = value / tick_size
            steps = (int(steps + 1 - 1e-9) if up else int(steps + 1e-9))
            return round(steps * tick_size, 10)

        wanted = round(round(price / tick_size) * tick_size, 10)
        point = getattr(info, 'point', 0) or tick_size
        stops = getattr(info, 'trade_stops_level', 0) or 0
        gap = max(stops * point, tick_size)

        tick = mt5.symbol_info_tick(symbol)
        bid = getattr(tick, 'bid', 0) if tick else 0
        ask = getattr(tick, 'ask', 0) if tick else 0
        if not bid or not ask:
            return wanted, None          # no book to measure against

        if side is OrderSide.BUY:
            limit = to_tick(ask - gap)           # must be BELOW the ask
            if wanted <= limit:
                return wanted, None
            return limit, (f"buy limit moved {wanted:.5f} -> {limit:.5f}: "
                           f"{symbol} requires {gap:.5f} below the "
                           f"{ask:.5f} ask")
        limit = to_tick(bid + gap, up=True)      # must be ABOVE the bid
        if wanted >= limit:
            return wanted, None
        return limit, (f"sell limit moved {wanted:.5f} -> {limit:.5f}: "
                       f"{symbol} requires {gap:.5f} above the "
                       f"{bid:.5f} bid")

    def place_pending_limit(self, symbol, side, volume, price, comment="",
                            position_ticket=None):
        """Rest a limit order. With position_ticket, the limit CLOSES
        that position when it executes (hedging-mode limit exits).

        Price must be rounded to trade_tick_size and far enough from the
        book (see legal_limit_price) or brokers reject with Invalid
        Price (10015) — live-tested 2026-06 and again 2026-08."""
        try:
            price, moved = self.legal_limit_price(symbol, side, price)
            if moved:
                logging.info("%s", moved)

            request = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY_LIMIT if side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL_LIMIT),
                "price": price,
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._pending_filling_mode(symbol),
            }
            if position_ticket:
                request["position"] = int(position_ticket)
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = (mt5.last_error() if result is None
                         else f"{result.retcode} - {result.comment}")
                return {'ok': False, 'ticket': None, 'error': str(error)}
            return {'ok': True, 'ticket': result.order, 'error': None,
                    'price': price, 'price_note': moved}
        except Exception as e:
            return {'ok': False, 'ticket': None, 'error': str(e)}

    def pending_orders_by_magic(self, symbol=None):
        """Pending orders created by THIS system. Used to sweep stale
        orders before a new execution — orphan pendings accumulate
        after timeouts and failed cancels (live-tested 2026-06)."""
        if mt5 is None:
            return []
        raw = (mt5.orders_get(symbol=symbol) if symbol
               else mt5.orders_get()) or ()
        return [{'ticket': o.ticket, 'symbol': o.symbol,
                 'volume': getattr(o, 'volume_current', 0.0),
                 'price': getattr(o, 'price_open', 0.0)}
                for o in raw if o.magic == MAGIC_NUMBER]

    def modify_pending(self, ticket, price):
        """Re-peg a resting limit in place — no cancel/replace round trip.

        The re-peg is subject to the SAME minimum-distance rule as the
        original placement, so it is legalised the same way. Without
        this, a symbol with a stops level lets the order rest and then
        rejects every attempt to chase the market with it."""
        try:
            order = next(iter(mt5.orders_get(ticket=ticket) or ()), None)
            if order is not None:
                side = (OrderSide.BUY
                        if getattr(order, 'type', None)
                        == mt5.ORDER_TYPE_BUY_LIMIT else OrderSide.SELL)
                price, moved = self.legal_limit_price(
                    order.symbol, side, price)
                if moved:
                    logging.info("%s", moved)
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_MODIFY,
                "order": ticket,
                "price": price,
            })
            ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
            return {'ok': ok,
                    'error': None if ok else
                    (str(mt5.last_error()) if result is None
                     else f"{result.retcode} - {result.comment}")}
        except Exception as e:
            return {'ok': False, 'error': str(e)}

    def cancel_pending(self, ticket):
        """Remove a resting order, then ALWAYS report what filled first —
        a 'cancelled' order can carry partial fills, and the deal
        history can lag briefly after a cancel (live-tested 2026-06),
        so a zero-fill result is re-read once."""
        try:
            result = mt5.order_send({
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": ticket,
            })
            state = self.order_fill_state(ticket)
            if state['filled_volume'] == 0 and not state['still_open']:
                time.sleep(0.05)   # deal history lag after cancel
                state = self.order_fill_state(ticket)
            if state.get('from_position'):
                state['leaked_fill'] = True
            if state['filled_volume'] == 0:
                # Deal history can still be behind. MT5 turns a filled
                # pending order into a POSITION carrying the same
                # ticket, and positions_get never lags — this is the
                # authoritative "it actually filled" check. Missing it
                # reports a clean cancel while a live position sits on
                # the book (seen 2026-08-06: a 'cancelled' scenario
                # order reappeared as an orphan seconds later).
                for position in (mt5.positions_get(ticket=int(ticket))
                                 or ()):
                    state.update({
                        'filled_volume': position.volume,
                        'price': position.price_open,
                        'position_tickets': [position.ticket],
                        'still_open': False,
                        'leaked_fill': True,
                    })
            state['cancelled'] = (result is not None and
                                  result.retcode == mt5.TRADE_RETCODE_DONE)
            return state
        except Exception as e:
            state = self.order_fill_state(ticket)
            state['cancelled'] = False
            state['error'] = str(e)
            return state

    def order_fill_state(self, ticket):
        """Filled volume / VWAP / position tickets for an order, from the
        deal history (works for pending and market orders alike)."""
        filled = 0.0
        notional = 0.0
        position_tickets = []
        # Whether the fill was found as a POSITION rather than a deal.
        # cancel_pending turns this into `leaked_fill`: a cancel that
        # did not prevent a fill is a distinct event and has to stay
        # visible in the report, not be smoothed into a normal fill.
        from_position = False
        try:
            deals = mt5.history_deals_get(ticket=ticket) or ()
            for deal in deals:
                if deal.order != ticket:
                    continue
                filled += deal.volume
                notional += deal.volume * deal.price
                if deal.position_id and deal.position_id not in position_tickets:
                    position_tickets.append(deal.position_id)
            still_open = bool(mt5.orders_get(ticket=ticket))
            if not filled and not still_open:
                # Gone from the book with no deal recorded yet. MT5
                # turns a filled pending into a POSITION carrying the
                # ORDER's ticket, and positions_get shows it BEFORE
                # deal history does — the same lag cancel_pending
                # already works around.
                #
                # Reading deals alone therefore called a real fill "no
                # fill", so the scenario went down leak recovery, which
                # flattens at once: live 2026-08-10 a 120-second hold
                # closed in nine seconds because of it. The position IS
                # the fill; report it as one.
                for position in (mt5.positions_get(ticket=int(ticket))
                                 or ()):
                    filled += position.volume
                    notional += position.volume * position.price_open
                    if position.ticket not in position_tickets:
                        position_tickets.append(position.ticket)
                        from_position = True
        except Exception as e:
            return {'ok': False, 'filled_volume': filled, 'price': None,
                    'position_tickets': position_tickets,
                    'still_open': False, 'error': str(e)}
        vwap = notional / filled if filled > 0 else None
        return {'ok': True, 'filled_volume': filled, 'price': vwap,
                'position_tickets': position_tickets,
                'from_position': from_position,
                'still_open': still_open, 'error': None}

    def close_position_ticket(self, symbol, ticket, volume, entry_side,
                              slippage_points=1.0, comment=""):
        """Close a specific position by ticket. REQUIRED on hedging-mode
        accounts, where a plain opposite order would open a second
        position instead of closing this one."""
        try:
            tick = self.symbol_tick(symbol)
            info = self.symbol_info(symbol)
            if not tick or not info:
                return OrderResult(False, error=f"No market data for {symbol}")
            close_side = entry_side.opposite
            price = tick.ask if close_side is OrderSide.BUY else tick.bid
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY if close_side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL),
                "position": ticket,
                "price": price,
                "deviation": int(slippage_points / info.point),
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            result, _mode = self._send_market(request, symbol)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                error = (mt5.last_error() if result is None
                         else f"{result.retcode} - {result.comment}")
                if result is not None and result.retcode == 10030:
                    error += f" (tried every filling mode; " \
                             f"{self._filling_hint(symbol)})"
                return OrderResult(False, requested_price=price,
                                   error=f"Close failed: {error}")
            return OrderResult(True, requested_price=price,
                               executed_price=result.price,
                               ticket=result.order,
                               volume=getattr(result, 'volume', volume))
        except Exception as e:
            return OrderResult(False, error=f"Close error: {e}")

    def send_market_order(self, symbol, side, volume,
                          slippage_points=1.0, comment=""):
        """Send an IOC market order; returns OrderResult."""
        try:
            info = self.ensure_symbol(symbol)
            if not info:
                return OrderResult(False, error=f"Symbol {symbol} not found")

            tick = self.symbol_tick(symbol)
            if not tick:
                return OrderResult(False, error=f"No tick data for {symbol}")

            price = tick.ask if side is OrderSide.BUY else tick.bid
            deviation = int(slippage_points / info.point)

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": (mt5.ORDER_TYPE_BUY if side is OrderSide.BUY
                         else mt5.ORDER_TYPE_SELL),
                "price": price,
                "deviation": deviation,
                "magic": MAGIC_NUMBER,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }

            result, _mode = self._send_market(request, symbol)
            if result is None:
                return OrderResult(False, requested_price=price,
                                   error=f"order_send failed: {mt5.last_error()}")
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                detail = f"{result.retcode} - {result.comment}"
                if result.retcode == 10030:
                    detail += f" (tried every filling mode; " \
                              f"{self._filling_hint(symbol)})"
                return OrderResult(False, requested_price=price,
                                   error=f"Order failed: {detail}")

            return OrderResult(True, requested_price=price,
                               executed_price=result.price,
                               ticket=result.order,
                               volume=getattr(result, 'volume', volume))

        except Exception as e:
            logging.error("Order exception on %s: %s", symbol, e)
            return OrderResult(False, error=f"Execution error: {e}")
