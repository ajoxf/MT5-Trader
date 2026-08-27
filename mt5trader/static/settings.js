/* Settings: the two accounts, and the pairs that route across them.
 *
 * This panel is the one part of the UI that must work with the ENGINE
 * DOWN. The coordinator will not start until the symbols are right, and
 * these are the tools for finding out — so everything here talks to the
 * leg runners directly and never through the coordinator.
 *
 * Two house rules do most of the work in here:
 *   - A warning nobody can act on is not a fix. Where the system can
 *     name the wrong field AND the right value, it offers a one-click
 *     correction — but it stays a click. Nothing is corrected silently.
 *   - Structural fields need a restart and say so; nothing else does.
 */

(function () {
  'use strict';

  var UI = window.MT5Trader;
  var DASH = UI.DASH;

  var local = {
    accounts: [],
    nextPort: '127.0.0.1:9101',
    pairs: {},
    editing: null,          // the pair key being edited, '' for a new one
    draft: {},              // the pair form's current values
    derived: null,          // what MT5 says about the draft's two legs
    symbols: {},            // account -> last search result
    tests: {},              // account -> last connectivity answer
    settings: null,         // the engine's tunables, with their defaults
    hot: [],                // ...and which of them apply without a restart
    connection: null,       // is the SYSTEM connected, in one answer
    timer: null
  };

  function el(id) { return document.getElementById(id); }

  function escape(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function api(path, options) {
    return fetch(path, options).then(function (response) {
      return response.json().then(function (body) {
        return {ok: response.ok && body.ok !== false, status: response.status,
                body: body};
      });
    });
  }

  function refresh() {
    return Promise.all([
      api('/api/accounts'), api('/api/pairs'), api('/api/settings')
    ]).then(function (results) {
      local.accounts = results[0].body.accounts || [];
      local.nextPort = results[0].body.next_free_port || local.nextPort;
      local.pairs = results[1].body.pairs || {};
      local.settings = results[2].body.settings || {};
      local.defaults = results[2].body.defaults || {};
      local.hot = results[2].body.hot || [];
      render();
    });
  }

  // -- the panel ---------------------------------------------------------

  function node() {
    var existing = document.querySelector('.window.settings');
    if (existing) { return existing; }
    var panel = document.createElement('section');
    panel.className = 'window settings';
    panel.innerHTML =
      '<div class="titlebar"><span class="swatch"></span>' +
      '<span class="title">Exchanges — accounts, pairs and settings</span>' +
      '<span class="winbtns"><button class="winbtn close">&times;</button>' +
      '</span></div>' +
      '<div class="settings-body">' +
      '<section class="trading"></section>' +
      '<section class="accounts"></section>' +
      '<section class="pairs"></section>' +
      '</div>' +
      '<div class="note">Accounts, symbols and the hedge ratio are ' +
      'STRUCTURAL: the launcher reads them at startup, so a change here ' +
      'takes effect when you restart it. Everything else on the ladder ' +
      '(mode, time in force, overnight, increment, quantity) applies at ' +
      'once.</div>';
    panel.querySelector('.close').addEventListener('click', function () {
      UI.closePanel(UI.panelId('settings'));
    });
    panel.addEventListener('click', onClick);
    panel.addEventListener('change', onChange);
    document.getElementById('desktop').appendChild(panel);
    refresh();
    refreshConnection();
    // The connection state is the one thing on this page that changes
    // without the operator doing anything.
    if (!local.timer) {
      local.timer = window.setInterval(function () {
        if (document.querySelector('.window.settings')) {
          refreshConnection();
        }
      }, 5000);
    }
    return panel;
  }

  function render(fromForm) {
    var panel = document.querySelector('.window.settings');
    if (!panel) { return; }
    // The ladders repaint three times a second. A form that repaints
    // with them loses whatever is half-typed into it — and a field that
    // empties itself under the operator is worse than no form at all.
    // So: keep what is in the pair form (it is the draft), and do not
    // touch a section the cursor is inside.
    //
    // `fromForm === false` says the draft was just changed in code — a
    // one-click correction the operator asked for — and re-reading the
    // form here would immediately undo it.
    if (fromForm !== false) { readDraft(); }
    redraw(panel.querySelector('.trading'), tradingHtml);
    redraw(panel.querySelector('.accounts'), accountsHtml);
    redraw(panel.querySelector('.pairs'), pairsHtml);
  }

  function redraw(section, build) {
    if (!section) { return; }
    if (isTyping(section)) { return; }
    section.innerHTML = build();
  }

  function isTyping(section) {
    var active = document.activeElement;
    if (!active || !section.contains(active)) { return false; }
    // A BUTTON holding focus is a click that just happened, and the
    // whole point of that click is usually to change what is drawn.
    // Only a field being typed into blocks the repaint.
    var tag = active.tagName;
    return tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA';
  }

  // -- how a click behaves ------------------------------------------------

  function tradingHtml() {
    var settings = local.settings;
    if (!settings) { return '<h3>Trading</h3><p>loading…</p>'; }
    var confirm = settings.CONFIRM_MARKET_CLICKS;
    var html = '<h3>Trading <small>what a click does, and how fast</small>' +
      '</h3><div class="fields trading-fields">';
    html += field('Market clicks',
      '<label class="check"><input type="checkbox" class="s-confirm"' +
      (confirm ? ' checked' : '') + '> ask before crossing</label>' +
      '<div class="hint">' + (confirm
        ? 'ON: every market click asks first. Slower, and deliberate.'
        : 'OFF (default): ONE CLICK IS ONE ORDER — a market click ' +
          'crosses both accounts immediately. The arming carries the ' +
          'weight instead: the mode badge, the tinted columns, the ' +
          'cursor.') + '</div>');
    html += field('Slippage protection (ticks)',
      '<input class="s-protection" type="number" min="0" step="0.5" ' +
      'value="' + escape(settings.MARKET_PROTECTION_TICKS) + '">' +
      '<div class="hint">A market click is market-WITH-protection: a ' +
      'fill worse than the clicked spread by more than this many ' +
      'increments is refused, and the ladder says why. 0 turns it off.' +
      '</div>');
    html += field('Ladder row height (px)',
      '<input class="s-rowheight" type="number" min="12" max="40" ' +
      'step="1" value="' + escape(settings.ROW_HEIGHT_PX) + '">' +
      '<div class="hint">17 is the reference screen\'s. A bigger target ' +
      'is a faster and safer click on a large monitor.</div>');
    html += field('Click drain (seconds)',
      '<input class="s-drain" type="number" min="0.005" max="1" ' +
      'step="0.005" value="' + escape(settings.COMMAND_POLL_SEC) + '">' +
      '<div class="hint">How often the engine picks clicks up, on its ' +
      'own thread. This is the click-to-order latency you feel; the ' +
      'price poll is separate and slower.</div>');
    html += field('Re-peg dead band (ticks)',
      '<input class="s-repeg" type="number" min="0" step="0.5" value="' +
      escape(settings.REPEG_DEAD_BAND_TICKS) + '">' +
      '<div class="hint">LIMIT mode only. Every re-peg loses queue ' +
      'position, so a tight band means never being at the front of a ' +
      'queue — which defeats quoting.</div>');
    html += field('Stale quote limit (seconds)',
      '<input class="s-stale" type="number" min="0" step="0.5" value="' +
      escape(settings.MAX_QUOTE_AGE_SEC) + '">' +
      '<div class="hint">A pair is only as good as its worse leg. 0 ' +
      'turns the guard off — it can withhold an order, never a close.' +
      '</div>');
    html += '</div><div class="actions">' +
      '<button class="btn save-settings">Apply</button>' +
      '<span class="hint">These apply to the running engine at once — ' +
      'no restart.</span></div>';
    return html;
  }

  // -- accounts -----------------------------------------------------------

  function accountsHtml() {
    var html = '<h3>Exchanges <small>one login, one terminal, one port — ' +
      'the three things that cannot be shared</small></h3>';
    html += connectionHtml();
    html += '<table class="grid-form"><thead><tr><th>Name</th>' +
      '<th>Terminal (terminal64.exe)</th><th>Login</th><th>Server</th>' +
      '<th>Runner endpoint</th><th>Password</th><th></th></tr></thead><tbody>';
    local.accounts.forEach(function (account) {
      html += accountRow(account);
    });
    html += accountRow({name: '', endpoint: local.nextPort, isNew: true});
    html += '</tbody></table>';
    return html;
  }

  function accountRow(account) {
    var clashes = [account.endpoint_clash, account.login_clash,
                   account.terminal_clash].filter(Boolean);
    var test = local.tests[account.name];
    var html = '<tr data-account="' + escape(account.name) + '"' +
      (account.isNew ? ' class="new"' : '') + '>';
    html += '<td>' + (account.isNew
      ? '<input class="f-name" placeholder="e.g. CFI Spot">'
      : escape(account.name)) + '</td>';
    html += '<td><input class="f-terminal" value="' +
      escape(account.terminal_path) + '" placeholder="blank = attach to ' +
      'whichever terminal is open"></td>';
    html += '<td><input class="f-login" value="' + escape(account.login) +
      '"></td>';
    html += '<td><input class="f-server" value="' + escape(account.server) +
      '"></td>';
    html += '<td><input class="f-endpoint" value="' +
      escape(account.endpoint) + '" placeholder="127.0.0.1:9101"></td>';
    html += '<td><input class="f-password" type="password" placeholder="' +
      (account.password_set ? 'set — type to replace' : 'not set') +
      '"><div class="hint">' + (account.isNew
        ? 'goes to .env under a key derived from the name'
        : '→ ' + escape(account.password_env) + ' in .env') +
      '</div></td>';
    html += '<td class="actions">' +
      '<button class="btn save-account">Save</button>' +
      (account.isNew ? '' :
        // The three questions an operator actually asks, in the order
        // they ask them.
        '<button class="btn connect-account" title="Is the leg runner ' +
        'there, and is its terminal logged in?">Connect</button>' +
        '<button class="btn test-account" title="Can this account ' +
        'trade — Algo Trading, permissions, hedging?">Test</button>' +
        '<button class="btn diagnose-account" title="Everything: the ' +
        'account, its symbols, and whether the two legs fit">Diagnose' +
        '</button>' +
        '<button class="btn danger delete-account">Delete</button>') +
      '</td></tr>';

    if (clashes.length) {
      html += '<tr class="problem"><td colspan="7">' +
        clashes.map(escape).join('<br>') + '</td></tr>';
    }
    if (test) {
      html += '<tr class="checklist"><td colspan="7">' +
        checklistHtml(test) + '</td></tr>';
    }
    return html;
  }

  function checklistHtml(result) {
    // The answer, as a checklist with a FIX on every failure.
    if (result.error && !result.checks) {
      return '<div class="problem-text">' + escape(result.error) + '</div>';
    }
    var overall = result.overall || (result.ok ? 'PASS' : 'FAIL');
    var html = '<div class="checklist-head ' + overall.toLowerCase() + '">' +
      escape(result.ran || '') + '<b>' + overall + '</b> — ' +
      result.passed + ' passed, ' + result.warnings + ' warning(s), ' +
      result.failed + ' failed' +
      (result.connected ? ' · CONNECTED' : '') + '</div>';
    html += '<table class="checks"><tbody>';
    (result.checks || []).forEach(function (check) {
      html += '<tr class="c-' + check.status.toLowerCase() + '">';
      html += '<td class="status">' + check.status + '</td>';
      html += '<td class="what">' + escape(check.name) + '</td>';
      html += '<td>' + escape(check.message);
      // A warning nobody can act on is not a fix: every failure carries
      // the step that makes it pass.
      if ((check.fix || []).length) {
        html += '<ul class="fix">';
        check.fix.forEach(function (step) {
          html += '<li>' + escape(step) + '</li>';
        });
        html += '</ul>';
      }
      html += '</td></tr>';
    });
    return html + '</tbody></table>';
  }

  function connectionHtml() {
    // Is the system connected — plainly, at the top, in one line.
    var state = local.connection;
    if (!state) { return '<div class="conn checking">checking…</div>'; }
    var html = '<div class="conn ' + (state.connected ? 'up' : 'down') +
      '"><b>' + (state.connected ? 'CONNECTED' : 'NOT READY') + '</b> ' +
      escape(state.summary);
    if ((state.blockers || []).length > 1) {
      html += '<ul>';
      state.blockers.forEach(function (blocker) {
        html += '<li>' + escape(blocker) + '</li>';
      });
      html += '</ul>';
    }
    var clock = state.broker_clock || {};
    if (clock.broker_time) {
      html += '<div class="hint">Broker time ' + escape(clock.broker_time) +
        ' · ' + escape(clock.note) + '</div>';
    } else if (clock.note) {
      html += '<div class="hint problem-text">' + escape(clock.note) +
        '</div>';
    }
    return html + '</div>';
  }

  // -- pairs ---------------------------------------------------------------

  function pairsHtml() {
    var html = '<h3>Pairs <small>each ladder is leg A on one account and ' +
      'leg B on the other; the spread is B − β × A</small></h3>';
    html += '<table class="grid-form"><thead><tr><th>Key</th><th>Name</th>' +
      '<th>Leg A</th><th>Leg B</th><th>β</th><th>Increment</th>' +
      '<th>Enabled</th><th></th></tr></thead><tbody>';
    Object.keys(local.pairs).forEach(function (key) {
      var pair = local.pairs[key];
      html += '<tr data-pair="' + escape(key) + '">';
      html += '<td>' + escape(key) + '</td>';
      html += '<td>' + escape(pair.name || key) + '</td>';
      html += '<td>' + legText(pair.leg_a) + '</td>';
      html += '<td>' + legText(pair.leg_b) + '</td>';
      html += '<td>' + escape(pair.hedge_ratio) +
        (pair.hedge_ratio_for
          ? '<div class="hint">stamped for ' + escape(pair.hedge_ratio_for) +
            '</div>'
          : '<div class="hint problem-text">not stamped for any pair</div>') +
        '</td>';
      html += '<td>' + escape(pair.increment === null ||
                              pair.increment === undefined
                              ? 'derived' : pair.increment) + '</td>';
      html += '<td>' + (pair.enabled === false ? 'no' : 'yes') + '</td>';
      html += '<td class="actions">' +
        '<button class="btn edit-pair">Edit</button>' +
        '<button class="btn danger delete-pair">Delete</button></td></tr>';
    });
    html += '</tbody></table>';
    html += '<button class="btn new-pair">New pair</button>';
    if (local.editing !== null) { html += pairForm(); }
    return html;
  }

  function legText(leg) {
    leg = leg || {};
    if (!leg.account && !leg.symbol) { return DASH; }
    return escape(leg.symbol) + '<div class="hint">on ' +
      escape(leg.account) + '</div>';
  }

  function pairForm() {
    var draft = local.draft;
    var derived = local.derived;
    var html = '<div class="pair-form"><h4>' +
      (local.editing ? 'Editing ' + escape(local.editing) : 'New pair') +
      '</h4><div class="fields">';
    html += field('Key', '<input class="p-key" value="' +
                  escape(draft.key) + '" placeholder="XAUUSD_|GC1226"' +
                  (local.editing ? ' disabled' : '') + '>');
    html += field('Name', '<input class="p-name" value="' +
                  escape(draft.name) + '">');
    html += field('Leg A', accountSelect('p-account-a', draft.leg_a_account) +
                  symbolInput('a', draft.leg_a_symbol));
    html += field('Leg B', accountSelect('p-account-b', draft.leg_b_account) +
                  symbolInput('b', draft.leg_b_symbol));
    html += field('Pair type',
                  '<select class="p-type">' +
                  option('SPOT_FUTURE', draft.pair_type) +
                  option('FUTURE_FUTURE', draft.pair_type) +
                  option('DIFFERENT', draft.pair_type) + '</select>' +
                  '<div class="hint">Same underlying → β is 1 and the ' +
                  'spread IS the basis. Different instruments on the same ' +
                  'price scale → β 1 and the spread is the differential. ' +
                  'Different scales → the price ratio.</div>');
    html += field('β (hedge ratio)',
                  '<input class="p-beta" type="number" step="0.000001" ' +
                  'value="' + escape(draft.hedge_ratio) + '">' +
                  (derived && derived.suggested_beta !== null &&
                   derived.suggested_beta !== undefined
                    ? '<div class="hint">' + escape(derived.beta_reason) +
                      ' <button class="btn tiny use-beta" data-value="' +
                      derived.suggested_beta + '">use ' +
                      derived.suggested_beta + '</button></div>'
                    : ''));
    html += field('Increment',
                  '<input class="p-increment" type="number" step="0.000001" ' +
                  'value="' + escape(draft.increment) +
                  '" placeholder="blank = derived">' +
                  (derived
                    ? '<div class="hint">' +
                      escape(derived.increment_derivation) + ' = ' +
                      escape(derived.increment) +
                      ' <button class="btn tiny use-increment" ' +
                      'data-value="' + derived.increment + '">use it</button>' +
                      '</div>'
                    : ''));
    html += field('Default quantity (spreads)',
                  '<input class="p-quantity" type="number" min="0" ' +
                  'step="0.01" value="' + escape(draft.default_quantity) +
                  '">' + (derived
                    ? '<div class="hint">' + escape(derived.clip_derivation) +
                      ', ' + UI.money(derived.spread_units) +
                      ' per 1.00 of spread</div>' : ''));
    html += field('Quoting leg (LIMIT mode)',
                  '<select class="p-quoting">' +
                  '<option value="">auto — the wider book</option>' +
                  option('a', draft.quoting_leg) +
                  option('b', draft.quoting_leg) + '</select>' +
                  (derived
                    ? '<div class="hint">measured widths: A ' +
                      UI.fmt(derived.widths.a, 4) + ' · B ' +
                      UI.fmt(derived.widths.b, 4) + ' — ' +
                      escape(derived.quoting_note) + '</div>'
                    : ''));
    html += field('Enabled',
                  '<input class="p-enabled" type="checkbox"' +
                  (draft.enabled === false ? '' : ' checked') + '>');
    html += '</div>';

    if (derived) {
      html += '<div class="derived">';
      html += '<div>Spread now: <b>' + UI.fmt(derived.spread_now, 4) +
        '</b></div>';
      html += '<div>Minimum this pair can trade: <b>' +
        (derived.min_notional_usd
          ? UI.money(derived.min_notional_usd) + ' a leg' : DASH) +
        '</b> — state it before anyone tries to trade under it</div>';
      html += '<div>Contract sizes read from MT5: A ' +
        escape((derived.specs.a || {}).contract_size) + ' · B ' +
        escape((derived.specs.b || {}).contract_size) +
        ' (never typed in)</div>';
      html += '</div>';
    }
    html += '<div class="actions">' +
      '<button class="btn derive-pair">Read both legs from MT5</button>' +
      '<button class="btn save-pair">Save pair</button>' +
      '<button class="btn cancel-pair">Cancel</button></div></div>';
    return html;
  }

  function field(label, control) {
    return '<label class="field"><span>' + label + '</span>' + control +
      '</label>';
  }

  function option(value, selected) {
    return '<option value="' + value + '"' +
      (value === selected ? ' selected' : '') + '>' + value + '</option>';
  }

  function accountSelect(cls, selected) {
    var html = '<select class="' + cls + '"><option value="">account…' +
      '</option>';
    local.accounts.forEach(function (account) {
      if (!account.name) { return; }
      html += '<option value="' + escape(account.name) + '"' +
        (account.name === selected ? ' selected' : '') + '>' +
        escape(account.name) + '</option>';
    });
    return html + '</select>';
  }

  function symbolInput(side, value) {
    var found = local.symbols[side] || null;
    var html = '<span class="symbol-row">' +
      '<input class="p-symbol-' + side + '" value="' + escape(value) +
      '" placeholder="symbol">' +
      '<button class="btn tiny find-symbol" data-side="' + side +
      '">Find</button></span>';
    if (found) {
      html += '<div class="hint symbols">';
      if (found.error) {
        html += escape(found.error);
      } else if (!found.symbols.length) {
        html += 'nothing on that account matches — it is probably the ' +
          'wrong account for this leg';
      } else {
        found.symbols.slice(0, 12).forEach(function (symbol) {
          html += '<button class="btn tiny pick-symbol" data-side="' + side +
            '" data-symbol="' + escape(symbol.symbol) + '">' +
            escape(symbol.symbol) + '</button> ';
        });
      }
      html += '</div>';
    }
    return html;
  }

  // -- events ---------------------------------------------------------------

  function onChange(e) {
    if (e.target.closest('.pair-form')) { readDraft(); }
  }

  function onClick(e) {
    var button = e.target.closest('button');
    if (!button) { return; }
    var row = button.closest('tr');

    if (button.classList.contains('save-settings')) {
      return saveSettings();
    }
    if (button.classList.contains('save-account')) {
      return saveAccount(row);
    }
    if (button.classList.contains('connect-account')) {
      return runCheck(row.dataset.account, 'connect', button);
    }
    if (button.classList.contains('test-account')) {
      return runCheck(row.dataset.account, 'test', button);
    }
    if (button.classList.contains('diagnose-account')) {
      return runCheck(row.dataset.account, 'diagnose', button);
    }
    if (button.classList.contains('delete-account')) {
      var target = row.dataset.account;
      return UI.ask('Delete account ' + target + '?',
        'The password stays in .env until you clear it there. A pair ' +
        'still routing to this account will refuse the deletion.',
        'Delete', function () {
          api('/api/accounts/' + encodeURIComponent(target),
              {method: 'DELETE'}).then(afterWrite);
        });
    }
    if (button.classList.contains('new-pair')) {
      local.editing = '';
      local.draft = {enabled: true, pair_type: 'SPOT_FUTURE',
                     hedge_ratio: 1.0, default_quantity: 1.0};
      local.derived = null;
      local.symbols = {};
      return render(false);
    }
    if (button.classList.contains('edit-pair')) {
      var key = row.dataset.pair;
      var pair = local.pairs[key] || {};
      local.editing = key;
      local.draft = {
        key: key, name: pair.name, pair_type: pair.pair_type || 'SPOT_FUTURE',
        hedge_ratio: pair.hedge_ratio, increment: pair.increment,
        default_quantity: pair.default_quantity,
        quoting_leg: pair.quoting_leg || '',
        enabled: pair.enabled !== false,
        leg_a_account: (pair.leg_a || {}).account,
        leg_a_symbol: (pair.leg_a || {}).symbol,
        leg_b_account: (pair.leg_b || {}).account,
        leg_b_symbol: (pair.leg_b || {}).symbol
      };
      local.derived = null;
      local.symbols = {};
      return render(false);
    }
    if (button.classList.contains('delete-pair')) {
      var doomed = row.dataset.pair;
      return UI.ask('Delete pair ' + doomed + '?',
        'A pair with an open position refuses this — flatten it first.',
        'Delete', function () {
          api('/api/pairs/' + doomed, {method: 'DELETE'}).then(afterWrite);
        });
    }
    if (button.classList.contains('cancel-pair')) {
      local.editing = null;
      return render();
    }
    if (button.classList.contains('find-symbol')) {
      return findSymbols(button.dataset.side);
    }
    if (button.classList.contains('pick-symbol')) {
      readDraft();
      local.draft['leg_' + button.dataset.side + '_symbol'] =
        button.dataset.symbol;
      local.symbols[button.dataset.side] = null;
      return render(false);
    }
    if (button.classList.contains('use-beta')) {
      readDraft();
      local.draft.hedge_ratio = parseFloat(button.dataset.value);
      return render(false);
    }
    if (button.classList.contains('use-increment')) {
      readDraft();
      local.draft.increment = parseFloat(button.dataset.value);
      return render(false);
    }
    if (button.classList.contains('derive-pair')) { return derive(); }
    if (button.classList.contains('save-pair')) { return savePair(); }
  }

  function runCheck(name, kind, button) {
    var label = button.textContent;
    button.textContent = '…';
    button.disabled = true;
    return api('/api/accounts/' + encodeURIComponent(name) + '/' + kind)
      .then(function (result) {
        var body = result.body || {};
        body.ran = kind.charAt(0).toUpperCase() + kind.slice(1) + ': ';
        local.tests[name] = body;
        button.textContent = label;
        button.disabled = false;
        render(false);
        refreshConnection();
      })
      .catch(function (error) {
        button.textContent = label;
        button.disabled = false;
        UI.toast(kind + ' could not run: ' + error.message);
      });
  }

  function refreshConnection() {
    return api('/api/connection').then(function (result) {
      var was = local.connection && local.connection.connected;
      local.connection = result.body;
      // Say it ONCE when it becomes true, rather than every poll: a
      // banner that never changes is a banner nobody reads.
      if (local.connection.connected && was === false) {
        UI.toast('Connected — both accounts logged in, Algo Trading on, ' +
                 'prices arriving. You can trade.', 'ok');
      }
      render(false);
    });
  }

  function saveSettings() {
    var panel = document.querySelector('.window.settings .trading');
    function number(selector) {
      return parseFloat(panel.querySelector(selector).value);
    }
    var fields = {
      CONFIRM_MARKET_CLICKS: panel.querySelector('.s-confirm').checked,
      MARKET_PROTECTION_TICKS: number('.s-protection'),
      ROW_HEIGHT_PX: number('.s-rowheight'),
      COMMAND_POLL_SEC: number('.s-drain'),
      REPEG_DEAD_BAND_TICKS: number('.s-repeg'),
      MAX_QUOTE_AGE_SEC: number('.s-stale')
    };
    return api('/api/settings',
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({fields: fields})})
      .then(function (result) {
        if (!result.ok) { UI.toast(result.body.error); return; }
        var cold = result.body.restart_required || [];
        UI.toast(cold.length
          ? 'applied — except ' + cold.join(', ') + ', which the launcher ' +
            'only reads at startup'
          : 'applied to the running engine', 'ok');
        refresh();
      });
  }

  function saveAccount(row) {
    var name = row.dataset.account ||
      (row.querySelector('.f-name') || {}).value;
    if (!name) {
      UI.toast('an account needs a name before it can be saved');
      return;
    }
    var body = {
      terminal_path: row.querySelector('.f-terminal').value,
      login: row.querySelector('.f-login').value,
      server: row.querySelector('.f-server').value,
      endpoint: row.querySelector('.f-endpoint').value,
      password: row.querySelector('.f-password').value
    };
    return api('/api/accounts/' + encodeURIComponent(name),
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)}).then(function (result) {
      if (!result.ok) {
        // The refusal's own words — it names the field and, where it
        // can, the value to use instead.
        UI.toast(result.body.error);
        return;
      }
      UI.toast('saved ' + name + ' — restart the launcher for it to take ' +
               'effect', 'ok');
      refresh();
    });
  }

  function readDraft() {
    var form = document.querySelector('.pair-form');
    if (!form) { return; }
    function value(selector) {
      var input = form.querySelector(selector);
      return input ? input.value : '';
    }
    local.draft = {
      key: value('.p-key') || local.draft.key,
      name: value('.p-name'),
      pair_type: value('.p-type'),
      hedge_ratio: parseFloat(value('.p-beta')) || local.draft.hedge_ratio,
      increment: value('.p-increment') === ''
        ? null : parseFloat(value('.p-increment')),
      default_quantity: parseFloat(value('.p-quantity')) || 1.0,
      quoting_leg: value('.p-quoting'),
      enabled: form.querySelector('.p-enabled').checked,
      leg_a_account: value('.p-account-a'),
      leg_a_symbol: value('.p-symbol-a'),
      leg_b_account: value('.p-account-b'),
      leg_b_symbol: value('.p-symbol-b')
    };
  }

  function findSymbols(side) {
    readDraft();
    var account = local.draft['leg_' + side + '_account'];
    if (!account) {
      UI.toast('choose the account for leg ' + side.toUpperCase() +
               ' first — symbols are per broker');
      return;
    }
    var query = local.draft['leg_' + side + '_symbol'] || '';
    return api('/api/accounts/' + encodeURIComponent(account) +
               '/symbols?q=' + encodeURIComponent(query))
      .then(function (result) {
        local.symbols[side] = result.ok
          ? {symbols: result.body.symbols || []}
          : {error: result.body.error};
        render();
      });
  }

  function draftPayload() {
    var draft = local.draft;
    return {
      name: draft.name || draft.key,
      leg_a: {account: draft.leg_a_account, symbol: draft.leg_a_symbol},
      leg_b: {account: draft.leg_b_account, symbol: draft.leg_b_symbol},
      pair_type: draft.pair_type,
      hedge_ratio: draft.hedge_ratio,
      increment: draft.increment,
      default_quantity: draft.default_quantity,
      quoting_leg: draft.quoting_leg || null,
      enabled: draft.enabled
    };
  }

  function derive() {
    readDraft();
    var key = local.editing || local.draft.key;
    if (!key) { UI.toast('the pair needs a key first'); return; }
    return api('/api/pairs/' + key + '/derive',
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(draftPayload())})
      .then(function (result) {
        if (!result.ok) { UI.toast(result.body.error); return; }
        local.derived = result.body;
        render();
      });
  }

  function savePair() {
    readDraft();
    var key = local.editing || local.draft.key;
    if (!key) { UI.toast('the pair needs a key, e.g. XAUUSD_|GC1226'); return; }
    var payload = draftPayload();
    if (!payload.leg_a.symbol || !payload.leg_b.symbol) {
      UI.toast('both legs need a symbol — Find lists what each account '
               + 'actually offers');
      return;
    }
    return api('/api/pairs/' + key,
               {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)})
      .then(function (result) {
        if (!result.ok) { UI.toast(result.body.error); return; }
        UI.toast(result.body.restart_required
          ? 'saved ' + key + ' — symbols, accounts and β are structural, so ' +
            'restart the launcher'
          : 'saved ' + key, 'ok');
        local.editing = null;
        refresh();
      });
  }

  function afterWrite(result) {
    if (!result.ok) { UI.toast(result.body.error); return; }
    refresh();
  }

  window.MT5Settings = {render: function () { node(); render(true); },
                        refresh: refresh, state: local};
})();
