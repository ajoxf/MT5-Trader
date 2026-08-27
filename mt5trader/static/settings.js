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
    tests: {}               // account -> last connectivity answer
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
      api('/api/accounts'), api('/api/pairs')
    ]).then(function (results) {
      local.accounts = results[0].body.accounts || [];
      local.nextPort = results[0].body.next_free_port || local.nextPort;
      local.pairs = results[1].body.pairs || {};
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
      '<span class="title">Settings — accounts and pairs</span>' +
      '<span class="winbtns"><button class="winbtn close">&times;</button>' +
      '</span></div>' +
      '<div class="settings-body">' +
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

  // -- accounts -----------------------------------------------------------

  function accountsHtml() {
    var html = '<h3>MT5 accounts <small>one login, one terminal, one ' +
      'port — the three things that cannot be shared</small></h3>';
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
        '<button class="btn test-account">Test</button>' +
        '<button class="btn danger delete-account">Delete</button>') +
      '</td></tr>';

    if (clashes.length) {
      html += '<tr class="problem"><td colspan="7">' +
        clashes.map(escape).join('<br>') + '</td></tr>';
    }
    if (test) {
      html += '<tr class="' + (test.ok ? 'good' : 'problem') +
        '"><td colspan="7">' + escape(testText(test)) + '</td></tr>';
    }
    return html;
  }

  function testText(test) {
    if (test.error) { return test.error; }
    var terminal = test.terminal || {};
    var account = test.account || {};
    var parts = [];
    parts.push(terminal.logged_in
      ? 'logged in as ' + terminal.login + ' on ' + terminal.server
      : 'terminal reachable, not logged in');
    if (account && account.balance !== undefined) {
      // Equity, not balance: brokers often fund a demo with CREDIT, and
      // balance alone then reads as an empty account.
      parts.push('equity ' + UI.money(account.equity) +
                 ' (balance ' + UI.money(account.balance) + ')');
    }
    parts.push(terminal.hedging ? 'hedging mode' : 'NETTING mode');
    (test.problems || []).forEach(function (problem) { parts.push(problem); });
    return parts.join(' · ');
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

    if (button.classList.contains('save-account')) {
      return saveAccount(row);
    }
    if (button.classList.contains('test-account')) {
      var name = row.dataset.account;
      return api('/api/accounts/' + encodeURIComponent(name) + '/test')
        .then(function (result) {
          local.tests[name] = result.body;
          render();
        });
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
