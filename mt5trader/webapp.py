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
import os
import time

from flask import Flask, jsonify, render_template, request

from . import config as cfg
from .commands import CommandLog
from .legs import RemoteLeg

#: How old the status file may be before the UI says the engine is not
#: running. Six polls at the default 0.3s: long enough not to flicker,
#: short enough that a dead coordinator is not mistaken for a quiet
#: market — which is the one confusion that gets orders clicked into a
#: screen nothing is behind.
STATUS_STALE_SEC = 2.0


def create_app(status_path='status.json', command_path='commands.jsonl',
               results_path='results.json', config_path='config.json'):
    app = Flask(__name__)
    commands = CommandLog(command_path)

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

    # -- pairs --------------------------------------------------------------

    @app.get('/api/pairs')
    def api_pairs():
        raw = cfg.load_raw(config_path)
        return jsonify({'pairs': raw.get('pairs') or {}})

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
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    app = create_app(args.status, args.commands, args.results, args.config)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == '__main__':
    main()
