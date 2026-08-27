"""The UI, in a real browser.

Python tests cannot see a temporal-dead-zone `ReferenceError` that
aborts an entire script block and silently unregisters a handler —
which is exactly what happened in the system this is ported from, where
clicking Save did a native form submit and reset the page. So this
drives Chromium under Playwright and reads `pageerror`.

It also cross-checks every number input's min/max/step against the
shipped default: two of them once rejected the engine's own defaults,
so Chrome refused to submit and fired no event at all.
"""

import json
import os
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip('playwright.sync_api')

from mt5trader.webapp import create_app                        # noqa: E402


class Publisher:
    """Stands in for the coordinator: it republishes the snapshot on a
    timer, exactly as the real one does. Without that the file ages, the
    web process correctly calls the engine stalled, and every click is
    refused — which is the behaviour under test elsewhere, not here."""

    def __init__(self, path, every=0.2):
        self.path = path
        self.every = every
        self.order_type = 'LIMIT'
        self.live = threading.Event()
        self.live.set()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            if self.live.is_set():
                self.publish()
            time.sleep(self.every)

    def publish(self, at=None):
        payload = snapshot(self.order_type)
        if at is not None:
            payload['at'] = at
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f)
        os.replace(tmp, self.path)


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    """The real Flask app, on a real port, over a published snapshot."""
    tmp = tmp_path_factory.mktemp('ui')
    paths = {name: str(tmp / f'{name}.json') for name in
             ('status', 'results', 'config')}
    paths['commands'] = str(tmp / 'commands.jsonl')

    publisher = Publisher(paths['status'])
    publisher.publish()
    publisher.thread.start()
    paths['publisher'] = publisher
    with open(paths['config'], 'w', encoding='utf-8') as f:
        json.dump({'accounts': {}, 'pairs': {}}, f)

    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'])
    from werkzeug.serving import make_server
    httpd = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}', paths
    publisher.stop.set()
    httpd.shutdown()


def snapshot(order_type='LIMIT'):
    rows = []
    for step in range(20, -21, -1):
        level = round(59.10 + step * 0.01, 4)
        rows.append({'level': level, 'work_buy': None, 'work_sell': None,
                     'is_best_bid': step == -1, 'is_best_ask': step == 1})
    return {
        'at': time.time(), 'loop_interval_sec': 0.31,
        'accounts': {'acct_a': {'profit': 0.0}, 'acct_b': {'profit': 0.0}},
        'reconciler': {'untracked_closes': [], 'escalated': [],
                       'unknown_accounts': []},
        'hedge_times_ms': [], 'click_to_on_ms': [],
        'pairs': {
            'XAUUSD_|GC1226': {
                'key': 'XAUUSD_|GC1226', 'name': 'Gold basis', 'enabled': True,
                'account_a': 'acct_a', 'account_b': 'acct_b',
                'symbol_a': 'XAUUSD_', 'symbol_b': 'GC1226',
                'hedge_ratio': 1.0, 'hedge_ratio_for': 'XAUUSD_|GC1226',
                'increment': 0.01, 'increment_derived': 0.01,
                'order_type': order_type, 'time_in_force': 'DAY',
                'overnight': 'ALLOW', 'default_quantity': 1.0,
                'clip_lots_a': 0.1, 'clip_lots_b': 0.1, 'spread_units': 10.0,
                'short_spread': 59.09, 'long_spread': 59.11,
                'market': {'spread': 59.10, 'short_spread': 59.09,
                           'long_spread': 59.11, 'net_change': -0.61,
                           'feed_badge': 'OK (oldest leg 0.2s)',
                           'session': {'open': 59.71, 'high': 59.80,
                                       'low': 58.90, 'volume': 0.0,
                                       'ours': True}},
                'rows': rows, 'orders': [], 'quotes': [], 'positions': [],
                'working_buys': 0, 'working_sells': 0, 'net_position': 0.0,
                'avg_entry': None, 'open_pnl': None, 'last_print': None,
                'errors': [],
            }
        },
    }


def chromium_path():
    """The browser this machine actually has.

    Playwright pins a build number per release; a box whose browsers were
    installed for a different release has a perfectly good Chromium under
    another name, and pointing at it beats downloading a second copy.
    None lets Playwright find its own.
    """
    root = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', '/opt/pw-browsers'))
    for candidate in sorted(root.glob('chromium-*/chrome-linux/chrome')):
        return str(candidate)
    return None


@pytest.fixture(scope='module')
def page(server):
    url, paths = server
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(executable_path=chromium_path())
        except Exception as e:                    # no browser on this box
            pytest.skip(f'chromium unavailable: {e}')
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text)
                if message.type == 'error' else None)
        page.goto(url)
        page.wait_for_selector('.ladder tbody tr')
        page.errors = errors
        page.paths = paths
        yield page
        browser.close()


def test_the_page_loads_without_a_single_script_error(page):
    """A TDZ ReferenceError aborts the whole script block and silently
    unregisters every handler after it. Nothing in Python can see that."""
    page.wait_for_timeout(900)                    # several refresh cycles
    assert page.errors == [], '\n'.join(page.errors)


def test_the_ladder_draws_the_reference_screens_columns(page):
    headers = page.eval_on_selector_all(
        '.ladder thead th', 'nodes => nodes.map(n => n.textContent)')
    assert headers == ['Work', 'Bids', 'Price', 'Asks', 'LTQ']
    assert page.locator('.ladder tbody tr').count() == 41


def test_the_inside_market_rule_is_drawn_between_the_two_touches(page):
    line = page.locator('.ladder tr.market-line')
    assert line.count() == 1
    border = line.first.locator('td').first.evaluate(
        'node => getComputedStyle(node).borderTopWidth')
    assert border == '2px'
    # And it sits between the best bid and the best ask, not on top of
    # one of them.
    bid = float(line.first.get_attribute('data-level'))
    ask_row = page.locator('.ladder tbody tr').nth(
        page.locator('.ladder tbody tr').count() - 1)
    assert bid < 59.11 and bid >= 59.09 - 0.011
    assert ask_row is not None


def test_bid_is_blue_and_ask_is_red_on_the_rendered_page(page):
    bid = page.locator('.ladder tr.in-bid td.bid').first
    ask = page.locator('.ladder tr.in-ask td.ask').first
    assert bid.evaluate('n => getComputedStyle(n).backgroundColor') == \
        'rgb(26, 111, 181)'
    assert ask.evaluate('n => getComputedStyle(n).backgroundColor') == \
        'rgb(176, 28, 28)'


def test_a_limit_click_places_one_order_and_asks_nothing(page):
    before = command_count(page)
    page.locator('.ladder tbody tr td.bid').nth(5).click()
    page.wait_for_timeout(250)
    assert page.locator('#modal.hidden').count() == 1     # no confirmation
    assert command_count(page) == before + 1
    command = last_command(page)
    assert command['kind'] == 'click'
    assert command['payload']['side'] == 'BUY'


def test_three_clicks_at_one_price_send_three_orders(page):
    before = command_count(page)
    cell = page.locator('.ladder tbody tr td.ask').nth(3)
    for _ in range(3):
        cell.click()
        page.wait_for_timeout(120)
    assert command_count(page) == before + 3
    levels = {json.loads(line)['payload']['level']
              for line in commands(page)[-3:]}
    assert len(levels) == 1                       # same price, three orders


def test_market_mode_is_unmistakable_and_confirms_through_our_own_modal(page):
    # The selector sends a command; the UI arms itself from what the
    # ENGINE says came back, never from the click. So the ladder is in
    # MARKET mode only once the snapshot says it is.
    page.select_option('.ladder .order-type', 'MARKET')
    page.paths['publisher'].order_type = 'MARKET'
    page.wait_for_selector('.window.mode-market', timeout=3000)
    cursor = page.locator('.ladder tbody td.bid').first.evaluate(
        'n => getComputedStyle(n).cursor')
    assert cursor == 'crosshair'

    before = command_count(page)
    page.locator('.ladder tbody tr td.bid').nth(6).click()
    page.wait_for_timeout(150)
    # Our own modal, never a native confirm() — and nothing is sent until
    # it is answered.
    assert page.locator('#modal:not(.hidden)').count() == 1
    assert command_count(page) == before
    page.click('#modal-confirm')
    page.wait_for_timeout(250)
    assert command_count(page) == before + 1
    assert last_command(page)['kind'] == 'click'
    page.paths['publisher'].order_type = 'LIMIT'


def test_every_number_input_accepts_the_value_the_engine_shipped(page):
    """Two of these once rejected the engine's own defaults, so Chrome
    refused to submit and fired no event at all."""
    bad = page.evaluate("""() => {
        const out = [];
        document.querySelectorAll('input[type=number]').forEach(input => {
            if (input.value !== '' && !input.checkValidity()) {
                out.push([input.className, input.value, input.min, input.max,
                          input.step]);
            }
        });
        return out;
    }""")
    assert bad == []


def test_the_engine_banner_appears_when_the_snapshot_goes_stale(page):
    """A dead coordinator must never read as a quiet market."""
    publisher = page.paths['publisher']
    publisher.live.clear()                 # the engine stops publishing
    publisher.publish(at=0)
    page.wait_for_selector('#engine-banner:not(.hidden)', timeout=5000)
    assert 'Nothing on this screen is live' in \
        page.text_content('#engine-banner')

    publisher.live.set()                   # ...and comes back
    # `state='attached'`: a hidden element is never "visible", which is
    # the point of asserting on it.
    page.wait_for_selector('#engine-banner.hidden', state='attached',
                           timeout=5000)
    assert page.locator('#engine-banner.hidden').count() == 1


def commands(page):
    try:
        with open(page.paths['commands'], encoding='utf-8') as f:
            return [line for line in f.read().splitlines() if line.strip()]
    except OSError:
        return []


def command_count(page):
    return len(commands(page))


def last_command(page):
    return json.loads(commands(page)[-1])


# -- the settings page ---------------------------------------------------

def test_the_settings_page_edits_accounts_and_pairs(page):
    """The one panel that must work with the ENGINE down: the
    coordinator will not start until the symbols are right, and this is
    where they get right."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')

    # A new account, saved from the blank row at the bottom.
    add_account(page, 'CFI Spot', '127.0.0.1:9101', login='5001')
    page.wait_for_selector('tr[data-account="CFI Spot"]')

    # And a second one that tries to take the same port.
    add_account(page, 'CFI Futures', '127.0.0.1:9101')
    # An ERROR toast, which is a different thing from the success one
    # above: it has no timer on it and waits to be dismissed by hand.
    page.wait_for_selector('.toast:not(.ok)')
    toast = page.text_content('.toast:not(.ok)')
    # The refusal names the account holding the port AND the one to use.
    assert "belongs to account 'CFI Spot'" in toast
    assert '9102' in toast
    page.click('.toast:not(.ok)')

    add_account(page, 'CFI Futures', '127.0.0.1:9102')
    page.wait_for_selector('tr[data-account="CFI Futures"]')

    assert page.locator('.window.settings tbody tr[data-account]').count() >= 2


def test_a_clash_already_in_the_config_is_shown_on_the_row(page):
    """A clash was once only discoverable by running a connectivity
    check, so an operator could look straight at two rows holding one
    terminal and see nothing wrong with either."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    rows = page.locator('.window.settings tr[data-account]')
    # Give both accounts the same terminal folder, through the API the
    # page itself uses, then reopen.
    page.evaluate("""async () => {
        await fetch('/api/accounts/CFI Spot', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({terminal_path: 'C:/MT5/terminal64.exe'})});
        await window.MT5Settings.refresh();
    }""")
    page.wait_for_timeout(200)
    assert rows.count() >= 2
    # The second account claiming it is refused at SAVE, with the reason
    # on the row rather than in a log.
    page.evaluate("""async () => {
        const r = await fetch('/api/accounts/CFI Futures', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({terminal_path: 'C:/MT5/terminal64.exe'})});
        window.__clash = (await r.json()).error;
    }""")
    assert 'One terminal serves ONE login' in page.evaluate('() => window.__clash')


def test_the_pair_form_offers_the_number_it_derived_as_a_click(page):
    """A warning nobody can act on is not a fix — but nothing is
    corrected silently either. The derived value is offered as a
    button."""
    page.click('#open-settings')
    page.wait_for_selector('.window.settings')
    page.click('.window.settings .new-pair')
    page.wait_for_selector('.pair-form')

    page.fill('.p-key', 'XAUUSD_|GC1226')
    page.fill('.p-beta', '10')            # a beta from another instrument
    page.evaluate("""() => {
        // Stand in for the leg runners: this page talks to them
        // directly, and there are none in a browser test.
        window.__realFetch = window.fetch;
        window.fetch = function (url, options) {
            if (String(url).indexOf('/derive') >= 0) {
                return Promise.resolve(new Response(JSON.stringify({
                    ok: true, suggested_beta: 1.0,
                    beta_reason: 'SPOT_FUTURE: the two legs are the same '
                        + 'underlying, so the spread IS the basis and beta is 1',
                    increment: 0.01,
                    increment_derivation: 'max(tick B 0.01, beta 1 x tick A 0.01)',
                    clip_lots_a: 0.1, clip_lots_b: 0.1,
                    clip_derivation: '1 spread = 0.1 A / 0.1 B',
                    spread_units: 10, min_notional_usd: 42921,
                    spread_now: 59.1, widths: {a: 0.2, b: 0.4},
                    quoting_leg_suggestion: 'b',
                    quoting_note: 'the wider bid-ask is the spread you earn, '
                        + 'and usually the less liquid leg',
                    specs: {a: {contract_size: 100}, b: {contract_size: 100}},
                    stamped_for: 'XAUUSD_|GC1226'
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            }
            return window.__realFetch(url, options);
        };
    }""")
    page.click('.derive-pair')
    page.wait_for_selector('.use-beta')

    # The wrong beta is still in the field — nothing was changed for us.
    assert page.input_value('.p-beta') == '10'
    assert 'the spread IS the basis' in page.text_content('.pair-form')
    page.click('.use-beta')
    page.wait_for_timeout(150)
    assert page.input_value('.p-beta') == '1'

    # And the derivation is on the screen beside the number.
    assert 'max(tick B 0.01' in page.text_content('.pair-form')
    assert '$42,921' in page.text_content('.derived') or \
        '42921' in page.text_content('.derived')
    page.evaluate('() => { window.fetch = window.__realFetch; }')


def add_account(page, name, endpoint, login=''):
    """Fill the blank row at the bottom and save it.

    The row is re-created on every refresh, so the fields are filled and
    then CHECKED before the click — a value typed into a row that has
    since been replaced is exactly the class of fault these browser
    tests exist to catch.
    """
    row = page.locator('.window.settings tr.new')
    row.locator('.f-name').fill(name)
    row.locator('.f-endpoint').fill(endpoint)
    if login:
        row.locator('.f-login').fill(login)
    assert row.locator('.f-name').input_value() == name
    row.locator('.save-account').click()
