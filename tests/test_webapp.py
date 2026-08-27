"""The web process: it renders, and it asks. It never trades.

Plus the UI rules the spec makes non-negotiable, asserted as tests
rather than trusted: no CDN, no native dialogs, and a refusal that
carries its own words.
"""

import json
import re
from pathlib import Path

import pytest

from mt5trader import config as cfg
from mt5trader.webapp import create_app

STATIC = Path(__file__).resolve().parent.parent / 'mt5trader' / 'static'
TEMPLATES = Path(__file__).resolve().parent.parent / 'mt5trader' / 'templates'


@pytest.fixture
def paths(tmp_path):
    return {
        'status': str(tmp_path / 'status.json'),
        'commands': str(tmp_path / 'commands.jsonl'),
        'results': str(tmp_path / 'results.json'),
        'config': str(tmp_path / 'config.json'),
    }


@pytest.fixture
def client(paths):
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])
    app.config.update(TESTING=True)
    return app.test_client()


def write_status(paths, **overrides):
    import time
    snapshot = {
        'at': time.time(), 'loop_interval_sec': 0.3,
        'accounts': {'acct_a': {'profit': 0.0}},
        'pairs': {'A|B': {'key': 'A|B', 'name': 'A vs B', 'enabled': True,
                          'net_position': 0.0, 'working_buys': 0,
                          'working_sells': 0, 'positions': [], 'orders': [],
                          'rows': [], 'errors': []}},
    }
    snapshot.update(overrides)
    with open(paths['status'], 'w', encoding='utf-8') as f:
        json.dump(snapshot, f)
    return snapshot


def test_a_dead_coordinator_is_never_mistaken_for_a_quiet_market(client,
                                                                  paths):
    """That confusion is what gets orders clicked into a screen with
    nothing behind it."""
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'down'
    assert 'a click would go nowhere' in body['engine_note']

    write_status(paths, at=0)                    # an ancient snapshot
    body = client.get('/api/status').get_json()
    assert body['engine'] == 'stalled'
    assert 'Nothing on this screen is live' in body['engine_note']


def test_a_command_is_refused_while_the_engine_is_down(client, paths):
    """Refuse rather than queue: a command written while nothing is
    running would be executed by whatever starts next, at prices from
    another hour."""
    response = client.post('/api/command',
                           json={'kind': 'click',
                                 'payload': {'pair': 'A|B', 'side': 'BUY',
                                             'level': 1.0}})
    assert response.status_code == 409
    assert 'not running' in response.get_json()['error']
    assert not Path(paths['commands']).exists()


def test_a_command_reaches_the_log_when_the_engine_is_up(client, paths):
    write_status(paths)
    response = client.post('/api/command',
                           json={'kind': 'click',
                                 'payload': {'pair': 'A|B', 'side': 'BUY',
                                             'level': 58.4}})
    assert response.status_code == 200
    command_id = response.get_json()['id']

    written = [json.loads(line) for line in
               open(paths['commands'], encoding='utf-8').read().splitlines()]
    assert written[0]['kind'] == 'click'
    assert written[0]['payload']['level'] == 58.4

    # The result is pending until the coordinator has run it.
    assert client.get(f'/api/result/{command_id}').get_json()['pending']
    with open(paths['results'], 'w', encoding='utf-8') as f:
        json.dump({command_id: {'ok': True, 'data': {'order': {}}}}, f)
    assert client.get(f'/api/result/{command_id}').get_json()['ok'] is True


def test_saving_an_account_refuses_a_clash_with_the_reason_on_the_row(client,
                                                                      paths):
    cfg.save_raw(paths['config'], {
        'accounts': {'a': {'endpoint': '127.0.0.1:9101', 'login': 5001}},
        'pairs': {}})

    response = client.post('/api/accounts/b',
                           json={'endpoint': '127.0.0.1:9101'})
    assert response.status_code == 400
    assert "belongs to account 'a'" in response.get_json()['error']

    response = client.post('/api/accounts/b', json={'endpoint': 'nonsense'})
    assert response.status_code == 400
    assert 'COLON' in response.get_json()['error']


def test_a_password_goes_to_the_env_file_and_never_to_the_config(client,
                                                                 paths,
                                                                 tmp_path):
    cfg.save_raw(paths['config'], {'accounts': {'Live A': {}}, 'pairs': {}})
    client.post('/api/accounts/Live A',
                json={'endpoint': '127.0.0.1:9101', 'login': 5001,
                      'password': 'two words #1'})

    stored = json.load(open(paths['config'], encoding='utf-8'))
    assert 'password' not in stored['accounts']['Live A']
    env = (tmp_path / '.env').read_text(encoding='utf-8')
    assert 'MT5_PASSWORD_LIVE_A="two words #1"' in env


def test_the_accounts_page_shows_the_clash_on_the_row_itself(client, paths):
    cfg.save_raw(paths['config'], {
        'accounts': {'a': {'terminal_path': 'C:\\MT5\\terminal64.exe'},
                     'b': {'terminal_path': 'C:\\MT5\\terminal64.exe'}},
        'pairs': {}})
    rows = client.get('/api/accounts').get_json()['accounts']
    assert any(row['terminal_clash'] for row in rows)
    assert 'One terminal serves ONE login' in rows[0]['terminal_clash']


def test_a_pair_with_an_open_position_cannot_be_renamed_or_deleted(client,
                                                                   paths):
    """A leftover row is one resolving symbol away from a second live
    position — but a pair carrying money must not be removed out from
    under it either."""
    cfg.save_raw(paths['config'], {'accounts': {'a': {}},
                                   'pairs': {'A|B': {'name': 'A vs B'}}})
    write_status(paths, pairs={'A|B': {'net_position': -2.0}})

    response = client.post('/api/pairs/A|B', json={'name': 'renamed'})
    assert response.status_code == 409
    assert 'Flatten it first' in response.get_json()['error']

    response = client.delete('/api/pairs/A|B')
    assert response.status_code == 409

    # The control: flat, and both go through.
    write_status(paths, pairs={'A|B': {'net_position': 0.0}})
    assert client.post('/api/pairs/A|B', json={'name': 'renamed'}).status_code \
        == 200
    assert client.delete('/api/pairs/A|B').status_code == 200


def test_a_pair_key_with_a_slash_is_routable(client, paths):
    """The pair that most needs deleting has a slash in its key."""
    cfg.save_raw(paths['config'], {'accounts': {'a': {}},
                                   'pairs': {'XAU/USD|GC': {'name': 'x'}}})
    write_status(paths, pairs={})
    assert client.delete('/api/pairs/XAU/USD|GC').status_code == 200
    assert json.load(open(paths['config'], encoding='utf-8'))['pairs'] == {}


def test_symbol_search_says_how_to_start_the_runner_when_it_is_down(client,
                                                                    paths):
    """Symbol setup must work with the coordinator down — and when the
    RUNNER is down too, the answer is the command to start it, not a
    stack trace."""
    cfg.save_raw(paths['config'],
                 {'accounts': {'a': {'endpoint': '127.0.0.1:9199'}},
                  'pairs': {}})
    response = client.get('/api/accounts/a/symbols?q=XAU')
    assert response.status_code == 503
    assert 'run_leg.py' in response.get_json()['error']


def test_the_ui_loads_nothing_from_a_cdn():
    """A blocked CDN has taken this kind of UI down once. The dialog that
    reports 'could not save' must work when the network is what failed."""
    html = (TEMPLATES / 'index.html').read_text(encoding='utf-8')
    for source in re.findall(r'(?:src|href)="([^"]+)"', html):
        assert '//' not in source, source
    assert '@import' not in (STATIC / 'ladder.css').read_text(encoding='utf-8')


def strip_comments(script):
    """Code only. The house rule is written in a comment at the top of
    the file, and a guard that trips over its own documentation is a
    guard nobody keeps."""
    script = re.sub(r'/\*.*?\*/', '', script, flags=re.S)
    return re.sub(r'(^|\s)//[^\n]*', ' ', script)


def test_no_native_confirm_alert_or_prompt_ever_comes_back():
    """One shared modal, and a test that fails the build if they return."""
    script = strip_comments((STATIC / 'app.js').read_text(encoding='utf-8'))
    native = re.compile(r'(?<![\w.$])(confirm|alert|prompt)\s*\(')
    found = native.search(script)
    assert found is None, found.group(0)
    assert 'window.confirm' not in script
    assert 'window.alert' not in script


def test_the_ladder_is_the_reference_screens_five_columns():
    """`Work | Bids | Price | Asks | LTQ`, in that order — the layout is
    a specification, not a preference."""
    html = (TEMPLATES / 'index.html').read_text(encoding='utf-8')
    headers = re.findall(r'<th class="c-(\w+)">([^<]+)</th>', html)
    assert [name for name, _ in headers] == ['work', 'bid', 'price', 'ask',
                                             'ltq']
    assert [label for _, label in headers] == ['Work', 'Bids', 'Price',
                                               'Asks', 'LTQ']


def test_bid_is_blue_and_ask_is_red_everywhere():
    """The reference screen's convention, global: a price must not change
    colour depending on which table it sits in."""
    css = (STATIC / 'ladder.css').read_text(encoding='utf-8')
    assert '--bid: #1a6fb5' in css
    assert '--ask: #b01c1c' in css
    # Every place a bid or ask cell is painted uses those variables.
    for rule in re.findall(r'td\.(bid|ask)[^{]*\{[^}]*background:\s*([^;]+);',
                           css):
        assert 'var(--' + rule[0] in rule[1], rule


def test_the_inside_market_rule_is_drawn_crisply():
    """The single most important mark on the ladder: a rule across the
    full width, between the best bid and the best ask."""
    css = (STATIC / 'ladder.css').read_text(encoding='utf-8')
    assert re.search(r'tr\.market-line\s*>\s*td\s*\{\s*border-top:\s*2px '
                     r'solid var\(--inside\)', css)
    assert '--inside: #000000' in css


def test_market_mode_changes_the_click_columns():
    """The expensive misclick on any ladder is a market order the trader
    thought was a working order."""
    css = (STATIC / 'ladder.css').read_text(encoding='utf-8')
    assert '.window.mode-market td.bid' in css
    assert 'cursor: crosshair' in css


# -- the settings endpoints: they must work with the ENGINE down --------

class FakeRemoteLeg:
    """A leg runner that answers, without a runner or an MT5."""

    connected = True
    symbols = {
        'XAUUSD_': {'symbol': 'XAUUSD_', 'found': True, 'bid': 4292.00,
                    'ask': 4292.20, 'contract_size': 100.0, 'tick_size': 0.01,
                    'volume_min': 0.01, 'volume_step': 0.01,
                    'volume_max': 100.0},
        'GC1226': {'symbol': 'GC1226', 'found': True, 'bid': 4351.00,
                   'ask': 4351.40, 'contract_size': 100.0, 'tick_size': 0.01,
                   'volume_min': 0.10, 'volume_step': 0.10,
                   'volume_max': 100.0},
    }

    def __init__(self, name, endpoint, timeout=5.0):
        self.name = name

    def connect(self, retries=1, delay=0.0):
        return self.connected

    def close(self):
        pass

    def find_symbols(self, pattern, limit=40):
        return [dict(spec) for name, spec in self.symbols.items()
                if (pattern or '').upper() in name.upper()]

    def symbol_report(self, symbol):
        return dict(self.symbols.get(symbol)
                    or {'symbol': symbol, 'found': False,
                        'error': f'{symbol} does not exist on this broker'})

    def terminal_report(self):
        return {'library': True, 'terminal': True, 'logged_in': True,
                'algo_trading': True, 'hedging': True, 'login': 5001,
                'server': 'FakeServer'}

    def account_info(self):
        return {'account': self.name, 'balance': 0.0, 'equity': 5000.0,
                'profit': 0.0}


@pytest.fixture
def wired(client, paths, monkeypatch):
    """Two accounts saved, and a leg runner that answers."""
    from mt5trader import webapp
    cfg.save_raw(paths['config'], {
        'accounts': {'spot': {'endpoint': '127.0.0.1:9101'},
                     'fut': {'endpoint': '127.0.0.1:9102'}},
        'pairs': {}})
    monkeypatch.setattr(webapp, 'RemoteLeg', FakeRemoteLeg)
    return client


def test_testing_an_account_names_the_switch_that_is_off(wired, monkeypatch):
    """`10027 AutoTrading disabled by client` is a button in THAT
    terminal, and nothing else on the screen will say so."""
    body = wired.get('/api/accounts/spot/test').get_json()
    assert body['ok'] and body['problems'] == []
    assert body['terminal']['hedging'] is True

    monkeypatch.setattr(FakeRemoteLeg, 'terminal_report',
                        lambda self: {'logged_in': True, 'algo_trading': False,
                                      'hedging': True})
    body = wired.get('/api/accounts/spot/test').get_json()
    assert body['ok'] is False
    assert '10027' in body['problems'][0]


def test_a_netting_account_is_called_out_before_it_is_traded(wired,
                                                             monkeypatch):
    monkeypatch.setattr(FakeRemoteLeg, 'terminal_report',
                        lambda self: {'logged_in': True, 'algo_trading': True,
                                      'hedging': False})
    body = wired.get('/api/accounts/spot/test').get_json()
    assert any('NETTING' in problem for problem in body['problems'])


def test_a_symbols_contract_specs_come_from_mt5_not_from_a_form(wired):
    body = wired.get('/api/accounts/fut/symbol/GC1226').get_json()
    assert body['ok']
    assert body['report']['contract_size'] == 100.0
    assert body['report']['volume_min'] == 0.10

    missing = wired.get('/api/accounts/fut/symbol/GCZ4')
    assert missing.status_code == 404
    assert 'does not exist' in missing.get_json()['error']


def test_deriving_a_pair_shows_every_number_and_its_derivation(wired):
    body = wired.post('/api/pairs/XAUUSD_|GC1226/derive', json={
        'leg_a': {'account': 'spot', 'symbol': 'XAUUSD_'},
        'leg_b': {'account': 'fut', 'symbol': 'GC1226'},
        'pair_type': 'SPOT_FUTURE', 'hedge_ratio': 1.0}).get_json()

    assert body['ok']
    # Same underlying: beta is 1 and the spread IS the basis.
    assert body['suggested_beta'] == 1.0
    assert 'same' in body['beta_reason']
    assert body['spread_now'] == pytest.approx(59.10, abs=0.01)
    # max(tick B, beta x tick A), with the derivation beside it.
    assert body['increment'] == pytest.approx(0.01)
    assert 'max(tick B' in body['increment_derivation']
    # The matched minimum: leg B's 0.10 binds, so leg A is walked up.
    assert body['clip_lots_a'] == pytest.approx(0.10)
    assert body['clip_lots_b'] == pytest.approx(0.10)
    assert body['spread_units'] == pytest.approx(10.0)
    # Leg B's 0.10-lot minimum binds, priced off leg A's MID.
    assert body['min_notional_usd'] == pytest.approx(0.10 * 100 * 4292.10)
    # Which leg should quote, from MEASURED widths.
    assert body['widths']['b'] > body['widths']['a']
    assert body['quoting_leg_suggestion'] == 'b'
    assert 'less liquid' in body['quoting_note']
    assert body['stamped_for'] == 'XAUUSD_|GC1226'


def test_deriving_says_which_leg_is_wrong_rather_than_failing_blankly(wired):
    response = wired.post('/api/pairs/x/derive', json={
        'leg_a': {'account': 'spot', 'symbol': 'XAUUSD_'},
        'leg_b': {'account': 'fut', 'symbol': 'GCZ4'}})
    assert response.status_code == 404
    assert 'GCZ4' in response.get_json()['error']

    response = wired.post('/api/pairs/x/derive',
                          json={'leg_a': {'account': 'spot'}})
    assert response.status_code == 400
    assert 'leg A needs an account and a symbol' in response.get_json()['error']


def test_deriving_with_the_runner_down_says_how_to_start_it(wired,
                                                            monkeypatch):
    monkeypatch.setattr(FakeRemoteLeg, 'connected', False)
    response = wired.post('/api/pairs/x/derive', json={
        'leg_a': {'account': 'spot', 'symbol': 'XAUUSD_'},
        'leg_b': {'account': 'fut', 'symbol': 'GC1226'}})
    assert response.status_code == 503
    assert 'run_leg.py' in response.get_json()['error']


def test_an_account_still_carrying_a_pair_cannot_be_deleted(wired, paths):
    cfg.save_raw(paths['config'], {
        'accounts': {'spot': {'endpoint': '127.0.0.1:9101'},
                     'fut': {'endpoint': '127.0.0.1:9102'}},
        'pairs': {'A|B': {'leg_a': {'account': 'spot', 'symbol': 'X'},
                          'leg_b': {'account': 'fut', 'symbol': 'Y'}}}})

    response = wired.delete('/api/accounts/spot')
    assert response.status_code == 409
    assert 'A|B' in response.get_json()['error']

    # The control: with the pair gone, the account goes.
    wired.delete('/api/pairs/A|B')
    assert wired.delete('/api/accounts/spot').status_code == 200
    assert 'spot' not in cfg.load_raw(paths['config'])['accounts']


def test_the_settings_panel_ships_no_native_dialogs_either():
    script = strip_comments((STATIC / 'settings.js').read_text(encoding='utf-8'))
    native = re.compile(r'(?<![\w.$])(confirm|alert|prompt)\s*\(')
    found = native.search(script)
    assert found is None, found.group(0)
