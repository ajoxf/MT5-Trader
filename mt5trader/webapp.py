"""The web process: it renders, and it asks. It never trades.

The browser talks to this Flask app; the orders are placed by the
coordinator, which is the only process holding the legs. Everything the
UI shows comes from the coordinator's status snapshot, and everything
it does goes out as a command through `commands.py` — which is executed
once and never replayed.

Two rules from the spec shape the endpoints here:

- **Symbol setup must work with the coordinator DOWN.** Otherwise the
  system deadlocks: the coordinator will not start until the symbols
  are right, and these are the tools for finding out. So the symbol
  endpoints open a short-lived `RemoteLeg` straight to the account's
  runner.
- **Never send the operator to a log for a decision already made.** A
  refusal carries the broker's — or the config's — own words, in the
  response body, for the panel to print.
"""

import json
import logging
import os
import time

from flask import Flask, jsonify, render_template, request

from . import config as cfg, diagnostics, hedgeratio, sizing, \
    slippage
from .commands import CommandLog
from .legs import RemoteLeg

#: How old the status file may be before the UI says the engine is not
#: running. Six polls at the default 0.3s: long enough not to flicker,
#: short enough that a dead coordinator is not mistaken for a quiet
#: market — which is the one confusion that gets orders clicked into a
#: screen nothing is behind.
STATUS_STALE_SEC = 2.0


def create_app(status_path='status.json', command_path='commands.jsonl',
               results_path='results.json', config_path='config.json',
               db_path='mt5trader.db'):
    app = Flask(__name__)
    commands = CommandLog(command_path)

    def store():
        """A read-only view of the coordinator's database.

        Opened per request rather than held: SQLite in WAL mode lets a
        reader in while the writer works, and a connection kept across
        requests in a threaded server is a connection used from the
        wrong thread.
        """
        from .database import Store
        try:
            return Store(db_path)
        except Exception as e:
            logging.error('the journal could not be opened: %s', e)
            return None

    def read_json(path, default=None):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def status():
        snapshot = read_json(status_path)
        if snapshot is None:
            return {'engine': 'down',
                    'engine_note': ('the coordinator is not running — no '
                                    'prices, and a click would go nowhere'),
                    'pairs': {}, 'accounts': {}}
        age = time.time() - (snapshot.get('at') or 0)
        snapshot['status_age_sec'] = age
        if age > STATUS_STALE_SEC:
            snapshot['engine'] = 'stalled'
            snapshot['engine_note'] = (
                f'the last snapshot is {age:.1f}s old — the coordinator has '
                f'stopped or is stuck. Nothing on this screen is live.')
        else:
            snapshot['engine'] = 'up'
        return snapshot

    # -- what the panels render from ---------------------------------------

    @app.get('/')
    def index():
        return render_template('index.html')

    @app.get('/api/status')
    def api_status():
        return jsonify(status())

    @app.get('/api/config')
    def api_config():
        raw = cfg.load_raw(config_path)
        # The password is a NAME here, never a value (spec §10).
        return jsonify(raw)

    # -- what a click does --------------------------------------------------

    @app.post('/api/command')
    def api_command():
        payload = request.get_json(silent=True) or {}
        kind = payload.get('kind')
        if not kind:
            return jsonify({'ok': False, 'error': 'a command needs a kind'}), 400
        snapshot = status()
        if snapshot.get('engine') != 'up' and kind not in ('set_pair',):
            # Refuse rather than queue: a command written while nothing
            # is running would be executed by whatever starts next, at
            # prices from another hour.
            return jsonify({'ok': False, 'error': snapshot['engine_note']}), 409
        command_id = commands.submit(kind, payload.get('payload'))
        return jsonify({'ok': True, 'id': command_id})

    @app.get('/api/result/<command_id>')
    def api_result(command_id):
        results = read_json(results_path, {}) or {}
        result = results.get(command_id)
        if result is None:
            return jsonify({'ok': None, 'pending': True})
        return jsonify(result)

    @app.get('/api/settings')
    def api_settings():
        """Every tunable, with its default beside it.

        A guessed number gets corrected from measurement, and that needs
        it on screen first.
        """
        raw = cfg.load_raw(config_path)
        settings = dict(cfg.DEFAULT_SETTINGS)
        settings.update(raw.get('settings') or {})
        from .commands import CommandRunner
        return jsonify({'settings': settings,
                        'defaults': cfg.DEFAULT_SETTINGS,
                        'hot': sorted(CommandRunner.HOT_SETTINGS),
                        'structural': list(cfg.STRUCTURAL_SETTINGS)})

    @app.post('/api/settings')
    def api_save_settings():
        """Save settings, and make the hot ones true NOW.

        Written to config.json so they survive a restart, and pushed to
        the running coordinator as a command so the next click already
        obeys them. A setting that only takes effect on restart says so
        rather than looking applied.
        """
        payload = request.get_json(silent=True) or {}
        fields = payload.get('fields') or {}
        unknown = [name for name in fields if name not in cfg.DEFAULT_SETTINGS]
        if unknown:
            return jsonify({'ok': False,
                            'error': 'not a setting: ' + ', '.join(unknown)}), 400
        raw = cfg.load_raw(config_path)
        raw.setdefault('settings', {}).update(fields)
        cfg.save_raw(config_path, raw)

        from .commands import CommandRunner
        hot = {name: value for name, value in fields.items()
               if name in CommandRunner.HOT_SETTINGS}
        if hot and status().get('engine') == 'up':
            commands.submit('set_setting', {'fields': hot})
        cold = [name for name in fields if name not in hot]
        return jsonify({'ok': True, 'applied_now': sorted(hot),
                        'restart_required': sorted(cold)})

    # -- the journal: Fills, as the BROKER reported them ------------------

    @app.get('/api/fills')
    def api_fills():
        """The trade journal.

        Read from MT5's own deal history, so it carries the trader's
        manual terminal clicks as well as ours — `is_ours` says which is
        which. This is the record that survives the process.
        """
        db = store()
        if db is None:
            return jsonify({'ok': False,
                            'error': 'the journal database could not be '
                                     'opened — fills are still at the '
                                     'broker, but this table cannot show '
                                     'them'}), 503
        pair_key = request.args.get('pair') or None
        account = request.args.get('account') or None
        ours = request.args.get('ours') == '1'
        limit = min(int(request.args.get('limit', 200)), 2000)
        fills = db.fills(pair_key=pair_key, account=account, ours_only=ours,
                         limit=limit)
        return jsonify({'ok': True, 'fills': fills,
                        'totals': db.fill_totals(pair_key)})

    @app.get('/api/fills.csv')
    def api_fills_csv():
        """The same rows, for a spreadsheet. Broker time first, because
        that is the column that has to line up with MT5's History."""
        import csv
        import io
        db = store()
        if db is None:
            return 'the journal database could not be opened', 503
        fills = db.fills(pair_key=request.args.get('pair') or None,
                         limit=int(request.args.get('limit', 5000)))
        buffer = io.StringIO()
        columns = ['broker_time_ms', 'server_offset_s', 'account', 'symbol',
                   'pair_key', 'leg', 'side', 'entry', 'order_type', 'volume',
                   'price', 'commission', 'swap', 'profit', 'deal_id',
                   'order_id', 'position_ticket', 'is_ours', 'comment']
        writer = csv.DictWriter(buffer, fieldnames=columns,
                                extrasaction='ignore')
        writer.writeheader()
        for fill in fills:
            writer.writerow(fill)
        return (buffer.getvalue(), 200,
                {'Content-Type': 'text/csv',
                 'Content-Disposition': 'attachment; filename=fills.csv'})

    # -- the slippage report, over a real session --------------------------

    def slippage_report():
        """Build the report, or say why it cannot be built.

        Everything it needs is already recorded: the positions table
        holds the measured slippage at both ends, the status snapshot
        holds the measured broker offset that cuts the session, and the
        journal holds what the broker filled over the same stretch. This
        assembles them; it computes no price of its own.
        """
        db = store()
        if db is None:
            return None, ('the database could not be opened — the session '
                          'report is built from recorded positions, and '
                          'there are none to read')
        raw = cfg.load_raw(config_path)
        settings = dict(cfg.DEFAULT_SETTINGS)
        settings.update(raw.get('settings') or {})
        snapshot = status()
        offset = ((snapshot.get('broker_clock') or {}).get('offset_sec'))
        window = slippage.session_window(
            time.time(), offset,
            settings.get('OVERNIGHT_CLOSE_HOUR', 16),
            settings.get('OVERNIGHT_CLOSE_MINUTE', 55))
        if request.args.get('session') == 'all':
            window = dict(window, **{
                'from': None,
                'label': 'every session recorded',
                'note': ('every position in the database, not one session '
                         '— sessions are not comparable across a change of '
                         'symbol, ratio or clip size')})
        pair_key = request.args.get('pair') or None
        positions = db.positions_between(window['from'], window['to'],
                                         pair_key=pair_key)
        names = {key: (pair or {}).get('name') or key
                 for key, pair in (raw.get('pairs') or {}).items()}
        body = slippage.report(positions, window=window, names=names)
        # The journal over the SAME window, on the broker's stamps, as
        # the check on coverage: positions we know about against deals
        # the account actually saw.
        shift = (offset or 0) * 1000.0
        from_ms = None if window['from'] is None else \
            (window['from'] * 1000.0 + shift)
        try:
            body['journal'] = db.fills_between(
                from_ms, window['to'] * 1000.0 + shift, ours_only=True)
        except Exception as e:                    # a journal mid-write
            logging.error('the journal could not be counted: %s', e)
            body['journal'] = None
        return body, None

    @app.get('/api/slippage')
    def api_slippage():
        body, error = slippage_report()
        if body is None:
            return jsonify({'ok': False, 'error': error}), 503
        return jsonify(dict(body, ok=True))

    @app.get('/api/slippage.csv')
    def api_slippage_csv():
        """One row per position, both ends, for a spreadsheet.

        Empty cells, not zeros, where nothing was measured: a zero here
        would be read as a perfect fill by every tool that opens it.
        """
        import csv
        import io
        body, error = slippage_report()
        if body is None:
            return error, 503
        buffer = io.StringIO()
        columns = ['opened_at', 'closed_at', 'pair_key', 'side', 'quantity',
                   'order_type', 'entry_points', 'entry_money', 'exit_points',
                   'exit_money', 'round_trip_points', 'round_trip_money',
                   'click_to_on_ms', 'realized_pnl', 'position_id']
        writer = csv.DictWriter(buffer, fieldnames=columns,
                                extrasaction='ignore')
        writer.writeheader()
        for row in body['rows']:
            writer.writerow({column: ('' if row.get(column) is None
                                      else row.get(column))
                             for column in columns})
        return (buffer.getvalue(), 200,
                {'Content-Type': 'text/csv',
                 'Content-Disposition':
                     'attachment; filename=slippage.csv'})

    @app.get('/api/events')
    def api_events():
        db = store()
        if db is None:
            return jsonify({'ok': False, 'error': 'no database'}), 503
        return jsonify({'ok': True,
                        'events': db.events(request.args.get('kind') or None,
                                            int(request.args.get('limit',
                                                                 200)))})

    # -- account and symbol setup, which must work with the engine down ----

    @app.get('/api/accounts')
    def api_accounts():
        raw = cfg.load_raw(config_path)
        accounts = []
        for name, account in (raw.get('accounts') or {}).items():
            account = account or {}
            accounts.append({
                'name': name,
                'endpoint': account.get('endpoint'),
                'login': account.get('login'),
                'server': account.get('server'),
                'terminal_path': account.get('terminal_path'),
                'password_env': account.get('password_env')
                or cfg.env_key_for(name),
                'password_set': bool(os.environ.get(
                    account.get('password_env') or cfg.env_key_for(name))),
                # Shown ON the row: a clash was only discoverable by
                # running a connectivity check, so an operator could look
                # straight at two rows holding one terminal and see
                # nothing wrong with either.
                'endpoint_clash': cfg.endpoint_clash(
                    raw, name, (account.get('endpoint') or '').strip()),
                'login_clash': cfg.login_clash(raw, name,
                                               account.get('login')),
                'terminal_clash': cfg.terminal_clash(
                    raw, name, account.get('terminal_path')),
            })
        return jsonify({'accounts': accounts,
                        'next_free_port': cfg.next_free_port(raw)})

    @app.post('/api/accounts/<path:name>')
    def api_save_account(name):
        """Save one account — refusing a clash HERE, at save time.

        A refusal at save is a corrected field; a refusal at startup is
        five restart attempts with the reason scrolling past.
        """
        payload = request.get_json(silent=True) or {}
        raw = cfg.load_raw(config_path)
        raw.setdefault('accounts', {})
        account = raw['accounts'].setdefault(name, {})
        # What the row held BEFORE the edit: the guards must only refuse
        # a value being newly CLAIMED, or an existing clash is unfixable
        # because every save of either row trips over the other.
        was = {'terminal_path': (account.get('terminal_path') or '').strip(),
               'endpoint': (account.get('endpoint') or '').strip(),
               'login': str(account.get('login') or '')}

        endpoint = (payload.get('endpoint') or '').strip()
        if endpoint and endpoint != was['endpoint']:
            clash = cfg.endpoint_clash(raw, name, endpoint)
            if clash:
                return jsonify({'ok': False, 'error': clash}), 400
            try:
                from .ipc import parse_endpoint
                host, port = parse_endpoint(endpoint)
                endpoint = f'{host}:{port}'
            except ValueError as e:
                return jsonify({'ok': False, 'error': str(e)}), 400
        path = (payload.get('terminal_path') or '').strip()
        if path and path.lower() != was['terminal_path'].lower():
            clash = cfg.terminal_clash(raw, name, path)
            if clash:
                return jsonify({'ok': False, 'error': clash}), 400
        login = payload.get('login')
        if login and str(login) != was['login']:
            clash = cfg.login_clash(raw, name, login)
            if clash:
                return jsonify({'ok': False, 'error': clash}), 400

        account.update({
            'endpoint': endpoint or account.get('endpoint'),
            'terminal_path': path or account.get('terminal_path'),
            'login': int(login) if login else account.get('login'),
            'server': payload.get('server') or account.get('server'),
            'password_env': account.get('password_env')
            or cfg.env_key_for(name),
        })
        cfg.save_raw(config_path, raw)

        password = payload.get('password')
        if password:
            # Into .env, quoted, and NEVER into the config or a log line.
            env_path = os.path.join(os.path.dirname(
                os.path.abspath(config_path)) or '.', '.env')
            cfg.write_env_value(env_path, account['password_env'], password)
        return jsonify({'ok': True, 'account': name,
                        'restart_required': True,
                        'note': 'accounts are structural — restart the '
                                'launcher for this to take effect'})

    @app.get('/api/accounts/<path:name>/symbols')
    def api_find_symbols(name):
        """Search an account's symbols with the coordinator DOWN.

        Brokers spell gold XAUUSD, GOLD, XAUUSD.r — this is how the
        operator finds out which, and it must not need the engine that
        will not start until the answer is right.
        """
        raw = cfg.load_raw(config_path)
        account = (raw.get('accounts') or {}).get(name)
        if not account or not account.get('endpoint'):
            return jsonify({'ok': False,
                            'error': f"account '{name}' has no endpoint — "
                                     f"give it one, then start its leg "
                                     f"runner"}), 400
        leg = RemoteLeg(name, account['endpoint'], timeout=5.0)
        if not leg.connect(retries=1, delay=0.0):
            return jsonify({'ok': False,
                            'error': f"leg runner for '{name}' is not "
                                     f"answering at {account['endpoint']} — "
                                     f"start it with: python run_leg.py "
                                     f"--config config.json --account "
                                     f"{name}"}), 503
        try:
            found = leg.find_symbols(request.args.get('q', ''), 40)
            report = leg.terminal_report()
        finally:
            leg.close()
        return jsonify({'ok': True, 'symbols': found or [],
                        'terminal': report})

    @app.get('/api/accounts/<path:name>/connect')
    def api_connect_account(name):
        """Is the leg runner there, and is its terminal attached?

        The first question, and the one whose answer is usually an
        instruction: start the runner, open the terminal, log in.
        """
        leg, error, code = open_leg(name)
        checklist = diagnostics.Checklist()
        if leg is None:
            diagnostics.check_account(checklist, name, {'error': error})
            return jsonify(dict(checklist.result(), connected=False,
                                account=name)), code
        try:
            terminal = leg.terminal_report()
            offset = leg.server_offset()
        finally:
            leg.close()
        diagnostics.check_account(checklist, name, terminal, offset=offset)
        result = checklist.result()
        return jsonify(dict(result, account=name,
                            connected=bool(terminal.get('logged_in')),
                            terminal=terminal))

    @app.get('/api/accounts/<path:name>/test')
    def api_test_account(name):
        """Can this account TRADE?

        Everything Connect asks, plus the switches that decide whether
        an order will be accepted — in the operator's words, because
        `10027 AutoTrading disabled by client` is a button in that
        terminal and nothing else on the screen will say so.
        """
        leg, error, code = open_leg(name)
        checklist = diagnostics.Checklist()
        if leg is None:
            diagnostics.check_account(checklist, name, {'error': error})
            return jsonify(dict(checklist.result(), account=name)), code
        try:
            terminal = leg.terminal_report()
            account = leg.account_info()
            offset = leg.server_offset()
        finally:
            leg.close()
        diagnostics.check_account(checklist, name, terminal, account, offset)
        result = checklist.result()
        return jsonify(dict(result, account=name, terminal=terminal,
                            connected=bool(terminal.get('logged_in')),
                            problems=[check['message'] for check in
                                      result['checks']
                                      if check['status'] == 'FAIL']))

    @app.get('/api/accounts/<path:name>/diagnose')
    def api_diagnose_account(name):
        """Everything — the account, every symbol it carries, and every
        pair that routes through it.

        This is the one to run before the first trade of the day, and
        the one to read when something is refusing and nobody can say
        why.
        """
        raw = cfg.load_raw(config_path)
        leg, error, code = open_leg(name)
        checklist = diagnostics.Checklist()
        if leg is None:
            diagnostics.check_account(checklist, name, {'error': error})
            return jsonify(dict(checklist.result(), account=name)), code

        pairs = {key: cfg.PairConfig.from_dict(key, row)
                 for key, row in (raw.get('pairs') or {}).items()}
        reports = {}
        try:
            terminal = leg.terminal_report()
            account_info = leg.account_info()
            offset = leg.server_offset()
            diagnostics.check_account(checklist, name, terminal, account_info,
                                      offset)
            for key, pair in pairs.items():
                for symbol, role in ((pair.symbol_a, 'leg A'),
                                     (pair.symbol_b, 'leg B')):
                    account = (pair.account_a if role == 'leg A'
                               else pair.account_b)
                    if account != name or not symbol:
                        continue
                    if symbol not in reports:
                        reports[symbol] = leg.symbol_report(symbol)
                    diagnostics.check_symbol(checklist, name,
                                             reports[symbol], role)
        finally:
            leg.close()

        # The pair checks need BOTH legs, so the other account's runner
        # is opened too — and its absence is reported rather than
        # silently skipping the check.
        for key, pair in pairs.items():
            if name not in (pair.account_a, pair.account_b):
                continue
            other = pair.account_b if pair.account_a == name else pair.account_a
            other_leg, other_error, _code = open_leg(other)
            if other_leg is None:
                checklist.add(f'Pair {key}', 'Other leg', diagnostics.FAIL,
                              other_error,
                              ['Start that account\'s leg runner too — a '
                               'pair needs both'])
                continue
            try:
                report_a = (reports.get(pair.symbol_a)
                            or (other_leg.symbol_report(pair.symbol_a)
                                if pair.account_a == other else {}))
                report_b = (reports.get(pair.symbol_b)
                            or (other_leg.symbol_report(pair.symbol_b)
                                if pair.account_b == other else {}))
            finally:
                other_leg.close()
            diagnostics.check_pair(checklist, pair, report_a or {},
                                   report_b or {})

        return jsonify(dict(checklist.result(), account=name,
                            terminal=terminal,
                            connected=bool(terminal.get('logged_in'))))

    @app.get('/api/connection')
    def api_connection():
        """Is the SYSTEM connected — plainly, in one answer.

        The operator's real question is never "is endpoint 9101 bound";
        it is "can I trade right now". This answers that, and when the
        answer is no it names the one thing to fix.
        """
        raw = cfg.load_raw(config_path)
        snapshot = status()
        accounts = []
        blockers = []
        for name in (raw.get('accounts') or {}):
            leg, error, _code = open_leg(name)
            row = {'account': name, 'connected': False, 'trading': False,
                   'error': error}
            if leg is not None:
                try:
                    terminal = leg.terminal_report()
                finally:
                    leg.close()
                row.update({
                    'connected': bool(terminal.get('logged_in')),
                    'trading': bool(terminal.get('algo_trading')),
                    'login': terminal.get('login'),
                    'server': terminal.get('server'),
                    'error': None})
                if not row['connected']:
                    blockers.append(f'{name}: the terminal is not logged in')
                elif not row['trading']:
                    blockers.append(f'{name}: Algo Trading is off in that '
                                    f'terminal')
            else:
                blockers.append(f'{name}: {error}')
            accounts.append(row)

        if not accounts:
            blockers.append('no accounts are configured yet')
        if snapshot.get('engine') != 'up':
            blockers.append(snapshot.get('engine_note') or
                            'the coordinator is not running')

        feeds = []
        for key, pair in (snapshot.get('pairs') or {}).items():
            if not pair.get('enabled'):
                continue
            market = pair.get('market') or {}
            feeds.append({'pair': key, 'name': pair.get('name'),
                          'priced': market.get('spread') is not None,
                          'badge': market.get('feed_badge'),
                          'errors': pair.get('errors') or []})
            if market.get('spread') is None:
                blockers.append(f'{key}: no price yet')

        return jsonify({
            'ok': not blockers,
            'connected': not blockers,
            'accounts': accounts,
            'feeds': feeds,
            'engine': snapshot.get('engine'),
            'broker_clock': snapshot.get('broker_clock'),
            'blockers': blockers,
            'summary': ('Connected — both accounts logged in, Algo Trading '
                        'on, and prices arriving. You can trade.'
                        if not blockers else
                        'Not ready to trade: ' + blockers[0]),
        })

    @app.get('/api/accounts/<path:name>/symbol/<path:symbol>')
    def api_symbol(name, symbol):
        """One symbol's contract specs, read from MT5 — never typed in.

        Contract size, volume minimum and step and tick size are what the
        hedge arithmetic is built on; a typed-in contract size is a hedge
        error waiting for a different instrument.
        """
        leg, error, code = open_leg(name)
        if leg is None:
            return jsonify({'ok': False, 'error': error}), code
        try:
            report = leg.symbol_report(symbol)
        finally:
            leg.close()
        if not report.get('found'):
            return jsonify({'ok': False, 'error': report.get('error'),
                            'report': report}), 404
        return jsonify({'ok': True, 'report': report})

    def open_leg(name):
        """A short-lived RemoteLeg to one account, or why there is none.

        (leg, error, status) — the caller closes it. This is the path
        that must work with the COORDINATOR down.
        """
        raw = cfg.load_raw(config_path)
        account = (raw.get('accounts') or {}).get(name)
        if not account or not account.get('endpoint'):
            return None, (f"account '{name}' has no endpoint — give it one "
                          f"(e.g. {cfg.next_free_port(raw)}), then start its "
                          f"leg runner"), 400
        leg = RemoteLeg(name, account['endpoint'], timeout=5.0)
        if not leg.connect(retries=1, delay=0.0):
            return None, (f"leg runner for '{name}' is not answering at "
                          f"{account['endpoint']} — start it with: python "
                          f"run_leg.py --config config.json --account "
                          f"{name}"), 503
        return leg, None, 200

    @app.delete('/api/accounts/<path:name>')
    def api_delete_account(name):
        """Remove an account — but never one a pair is still routing to.

        A pair pointing at an account that no longer exists is a ladder
        that cannot trade and cannot say why.
        """
        raw = cfg.load_raw(config_path)
        if name not in (raw.get('accounts') or {}):
            return jsonify({'ok': False, 'error': f'no account {name}'}), 404
        used_by = [key for key, pair in (raw.get('pairs') or {}).items()
                   if name in ((pair.get('leg_a') or {}).get('account'),
                               (pair.get('leg_b') or {}).get('account'))]
        if used_by:
            return jsonify({'ok': False,
                            'error': f"'{name}' is still leg A or leg B of "
                                     f"{', '.join(used_by)}. Point those "
                                     f"pairs somewhere else (or delete them) "
                                     f"first."}), 409
        del raw['accounts'][name]
        cfg.save_raw(config_path, raw, allow_shrink=True)
        return jsonify({'ok': True, 'deleted': name,
                        'note': 'the password stays in .env until you clear '
                                'it there'})

    # -- pairs --------------------------------------------------------------

    @app.get('/api/pairs')
    def api_pairs():
        raw = cfg.load_raw(config_path)
        return jsonify({'pairs': raw.get('pairs') or {}})

    @app.post('/api/pairs/<path:key>/derive')
    def api_derive_pair(key):
        """Read both legs out of MT5 and derive what follows from them.

        Beta, the increment, the matched-minimum clip and the minimum
        tradable notional are all consequences of the two symbols'
        contract specs and live prices. Deriving them here means the
        operator sees the numbers — and the DERIVATION — before saving,
        instead of typing a beta that silently redefines the spread.
        """
        raw = cfg.load_raw(config_path)
        payload = request.get_json(silent=True) or {}
        pair = dict((raw.get('pairs') or {}).get(key) or {})
        pair.update(payload)
        legs = {}
        specs = {}
        for side in ('a', 'b'):
            leg_cfg = pair.get(f'leg_{side}') or {}
            name, symbol = leg_cfg.get('account'), leg_cfg.get('symbol')
            if not name or not symbol:
                return jsonify({'ok': False,
                                'error': f'leg {side.upper()} needs an '
                                         f'account and a symbol'}), 400
            leg, error, code = open_leg(name)
            if leg is None:
                return jsonify({'ok': False, 'error': error}), code
            legs[side] = leg
            try:
                specs[side] = leg.symbol_report(symbol)
            finally:
                leg.close()
            if not specs[side].get('found'):
                return jsonify({'ok': False,
                                'error': specs[side].get('error')}), 404

        price_a, price_b = _mid(specs['a']), _mid(specs['b'])
        suggested, why = hedgeratio.suggest(
            pair.get('pair_type', 'SPOT_FUTURE'), price_a, price_b)
        beta = float(pair.get('hedge_ratio') or suggested or 1.0)
        clip_a, clip_b = sizing.matched_minimum_lots(
            specs['a'].get('volume_min'), specs['b'].get('volume_min'),
            specs['a'].get('volume_step'), specs['b'].get('volume_step'),
            beta, specs['a'].get('contract_size'),
            specs['b'].get('contract_size'))
        tick_a = specs['a'].get('tick_size') or 0.0
        tick_b = specs['b'].get('tick_size') or 0.0
        increment = max(tick_b, beta * tick_a) if (tick_a and tick_b) else None
        floor = sizing.minimum_notional(
            specs['a'].get('contract_size'), specs['b'].get('contract_size'),
            price_a, price_b, beta, specs['a'].get('volume_min'),
            specs['b'].get('volume_min'))
        width_a = _width(specs['a'])
        width_b = _width(specs['b'])
        return jsonify({
            'ok': True,
            'specs': specs,
            'suggested_beta': suggested,
            'beta_reason': why,
            'stamped_for': hedgeratio.pair_signature(
                (pair.get('leg_a') or {}).get('symbol'),
                (pair.get('leg_b') or {}).get('symbol')),
            'increment': increment,
            'increment_derivation': (
                f'max(tick B {tick_b:g}, beta {beta:g} x tick A {tick_a:g})'
                if increment else 'both legs need a tick size from MT5'),
            'clip_lots_a': clip_a, 'clip_lots_b': clip_b,
            'clip_derivation': (
                f'1 spread = {clip_a:g} A / {clip_b:g} B — the smallest size '
                f'at which BOTH legs clear their own minimum volume '
                f'({specs["a"].get("volume_min")} and '
                f'{specs["b"].get("volume_min")} lots)'),
            'spread_units': sizing.spread_units(
                clip_b, specs['b'].get('contract_size')),
            'min_notional_usd': floor,
            'spread_now': (price_b - beta * price_a
                           if (price_a and price_b) else None),
            'widths': {'a': width_a, 'b': width_b},
            # Which leg SHOULD quote, from measurement rather than
            # assumption — and the tension stated, not hidden.
            'quoting_leg_suggestion': (
                'b' if (width_b or 0) >= (width_a or 0) else 'a'),
            'quoting_note': (
                'Default is the wider bid-ask — that is the spread you earn '
                'by quoting. It is usually also the less liquid leg, where '
                'you queue longest and fill least.'),
        })

    @app.post('/api/pairs/<path:key>')
    def api_save_pair(key):
        """Create or edit a pair. Routed on `<path:key>` because the pair
        that most needs deleting has a slash in its key."""
        payload = request.get_json(silent=True) or {}
        raw = cfg.load_raw(config_path)
        raw.setdefault('pairs', {})
        pair = raw['pairs'].setdefault(key, {})
        open_position = _open_position(status(), key)
        renaming = payload.get('name') and payload['name'] != pair.get('name')
        disabling = payload.get('enabled') is False and pair.get('enabled')
        if (renaming or disabling) and open_position:
            return jsonify({'ok': False, 'error': open_position}), 409
        for field in ('name', 'leg_a', 'leg_b', 'pair_type', 'hedge_ratio',
                      'increment', 'default_quantity', 'order_type',
                      'time_in_force', 'overnight', 'quoting_leg', 'enabled',
                      'rows', 'clip_lots_a', 'clip_lots_b'):
            if field in payload:
                pair[field] = payload[field]
        if 'hedge_ratio' in payload:
            # Beta belongs to the PAIR: stamp it, so a stale one from the
            # previous instrument cannot silently define the spread.
            from .hedgeratio import pair_signature
            pair['hedge_ratio_for'] = pair_signature(
                (pair.get('leg_a') or {}).get('symbol'),
                (pair.get('leg_b') or {}).get('symbol'))
        cfg.save_raw(config_path, raw)
        return jsonify({'ok': True, 'pair': key,
                        'restart_required': bool(
                            {'leg_a', 'leg_b', 'hedge_ratio', 'enabled'}
                            & set(payload))})

    @app.delete('/api/pairs/<path:key>')
    def api_delete_pair(key):
        raw = cfg.load_raw(config_path)
        if key not in (raw.get('pairs') or {}):
            return jsonify({'ok': False, 'error': f'no pair {key}'}), 404
        open_position = _open_position(status(), key)
        if open_position:
            return jsonify({'ok': False, 'error': open_position}), 409
        del raw['pairs'][key]
        # allow_shrink: this endpoint legitimately removes something.
        cfg.save_raw(config_path, raw, allow_shrink=True)
        return jsonify({'ok': True, 'deleted': key})

    return app


def _mid(report):
    bid, ask = report.get('bid'), report.get('ask')
    return ((bid + ask) / 2.0) if (bid and ask) else None


def _width(report):
    bid, ask = report.get('bid'), report.get('ask')
    return (ask - bid) if (bid and ask) else None


def _open_position(snapshot, key):
    """The refusal message for a pair that is not free to be changed.

    A leftover row is one resolving symbol away from a second live
    position on the same underlying — but a pair carrying money must not
    be renamed or deleted out from under it either.
    """
    row = (snapshot.get('pairs') or {}).get(key) or {}
    net = row.get('net_position') or 0.0
    if not net:
        return None
    return (f'{key} has {net:+g} spreads open. Flatten it first — renaming '
            f'or removing it now would leave the position with nothing '
            f'watching it.')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='MT5-Trader web UI')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--db', default='mt5trader.db')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    app = create_app(args.status, args.commands, args.results, args.config,
                     args.db)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
