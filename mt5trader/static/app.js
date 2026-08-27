/* The ladder, the Market Grid and the positions monitor.
 *
 * Everything on screen comes from ONE snapshot (/api/status), which the
 * coordinator publishes each poll — so the ladder cannot disagree with
 * the grid beside it, and the grid costs no extra MT5 round trips.
 * Everything the trader does goes out as a command (/api/command) and
 * is executed once by the coordinator.
 *
 * House rules, from the spec:
 *   - No native confirm() / alert() / prompt(). One shared modal.
 *   - Errors do not auto-hide. A failure that vanishes in 3s is missed.
 *   - Unmeasured is not zero: render an em dash.
 *   - Never send the operator to a log for a decision already made.
 */

(function () {
  'use strict';

  var REFRESH_MS = 300;
  var DASH = '—';

  var state = {
    snapshot: {pairs: {}, accounts: {}},
    open: [],            // panel ids in taskbar order
    active: null,
    armed: {},           // pair key -> quantity armed on the keypad
    locked: {},          // pair key -> scroll is locked
    filtered: {},        // pair key -> hide rows with nothing on them
    monitorTab: 'positions'
  };

  // -- plumbing -------------------------------------------------------

  function el(id) { return document.getElementById(id); }

  function fmt(value, digits) {
    if (value === null || value === undefined || value === '') return DASH;
    if (typeof value !== 'number') return String(value);
    if (!isFinite(value)) return DASH;
    return value.toFixed(digits === undefined ? 4 : digits);
  }

  function money(value) {
    if (value === null || value === undefined || !isFinite(value)) return DASH;
    var sign = value < 0 ? '-' : '';
    return sign + '$' + Math.abs(value).toFixed(2);
  }

  function toast(message, kind) {
    var box = document.createElement('div');
    box.className = 'toast' + (kind === 'ok' ? ' ok' : '');
    box.innerHTML = '<span class="dismiss">&times;</span>';
    box.appendChild(document.createTextNode(message));
    // Errors stay until dismissed, on purpose.
    box.addEventListener('click', function () { box.remove(); });
    el('toasts').appendChild(box);
    if (kind === 'ok') { window.setTimeout(function () { box.remove(); }, 4000); }
  }

  function ask(title, body, confirmLabel, onConfirm) {
    el('modal-title').textContent = title;
    el('modal-body').textContent = body;
    el('modal-confirm').textContent = confirmLabel;
    el('modal').classList.remove('hidden');
    el('modal').dataset.pending = '1';
    el('modal')._onConfirm = onConfirm;
  }

  function closeModal() {
    el('modal').classList.add('hidden');
    el('modal')._onConfirm = null;
  }

  function send(kind, payload, then) {
    return fetch('/api/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({kind: kind, payload: payload || {}})
    }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok || !data.ok) {
          // The refusal's OWN words, on screen. Never "check the log".
          toast(data.error || 'the command was refused');
          return null;
        }
        return pollResult(data.id, then);
      });
    }).catch(function (error) {
      toast('could not reach the web process: ' + error.message);
    });
  }

  function pollResult(id, then, tries) {
    tries = tries || 0;
    return fetch('/api/result/' + id).then(function (r) { return r.json(); })
      .then(function (result) {
        if (result && result.pending) {
          if (tries > 20) { return null; }
          return new Promise(function (resolve) {
            window.setTimeout(function () {
              resolve(pollResult(id, then, tries + 1));
            }, 150);
          });
        }
        if (result && result.ok === false) {
          toast(result.error || 'the engine refused that');
        } else if (result && result.data && result.data.reason) {
          toast(result.data.reason);
        } else if (result && result.data && result.data.refused) {
          toast(result.data.reason || 'refused');
        }
        if (then) { then(result); }
        return result;
      });
  }

  // -- panels ---------------------------------------------------------

  function panelId(kind, key) { return kind + ':' + (key || ''); }

  function openPanel(id) {
    if (state.open.indexOf(id) < 0) { state.open.push(id); }
    state.active = id;
    render();
  }

  function closePanel(id) {
    state.open = state.open.filter(function (other) { return other !== id; });
    render();
  }

  // -- the ladder ------------------------------------------------------

  function ladderNode(key) {
    var existing = document.querySelector('.ladder[data-pair="' + cssEscape(key) + '"]');
    if (existing) { return existing; }
    var node = el('ladder-template').content.firstElementChild.cloneNode(true);
    node.dataset.pair = key;
    wireLadder(node, key);
    el('desktop').appendChild(node);
    return node;
  }

  function cssEscape(value) { return String(value).replace(/"/g, '\\"'); }

  function wireLadder(node, key) {
    node.querySelector('.close').addEventListener('click', function () {
      closePanel(panelId('ladder', key));
    });
    node.querySelector('.order-type').addEventListener('change', function (e) {
      setPair(key, {order_type: e.target.value});
    });
    node.querySelector('.tif').addEventListener('change', function (e) {
      setPair(key, {time_in_force: e.target.value});
    });
    node.querySelector('.overnight').addEventListener('change', function (e) {
      setPair(key, {overnight: e.target.value});
    });
    node.querySelector('.increment').addEventListener('change', function (e) {
      setPair(key, {increment: parseFloat(e.target.value)});
    });
    node.querySelector('.default-qty').addEventListener('change', function (e) {
      setPair(key, {default_quantity: parseFloat(e.target.value)});
    });
    node.querySelector('.lock-scroll').addEventListener('change', function (e) {
      state.locked[key] = e.target.checked;
    });
    node.querySelector('.filter').addEventListener('change', function (e) {
      state.filtered[key] = e.target.checked;
      render();
    });
    node.querySelector('.keypad').addEventListener('click', function (e) {
      var button = e.target.closest('.qty');
      if (!button) { return; }
      state.armed[key] = button.dataset.qty ? parseFloat(button.dataset.qty) : null;
      render();
    });
    node.querySelector('.cxl-b').addEventListener('click', function () {
      send('cancel_where', {pair: key, side: 'BUY'});
    });
    node.querySelector('.cxl-s').addEventListener('click', function () {
      send('cancel_where', {pair: key, side: 'SELL'});
    });
    node.querySelector('.cxl-all').addEventListener('click', function () {
      send('cancel_where', {pair: key});
    });
    node.querySelector('tbody').addEventListener('click', function (e) {
      var cell = e.target.closest('td');
      if (!cell) { return; }
      var row = cell.closest('tr');
      var level = parseFloat(row.dataset.level);
      if (cell.classList.contains('bid')) {
        clickLevel(key, 'BUY', level);
      } else if (cell.classList.contains('ask')) {
        clickLevel(key, 'SELL', level);
      } else if (cell.classList.contains('work') && cell.dataset.orderId) {
        // Click the Work cell to pull ONE of the orders resting there.
        send('cancel_order', {order_id: cell.dataset.orderId});
      }
    });
  }

  function clickLevel(key, side, level) {
    var pair = state.snapshot.pairs[key] || {};
    var payload = {pair: key, side: side, level: level};
    if (state.armed[key]) { payload.quantity = state.armed[key]; }
    if (pair.order_type === 'MARKET') {
      // A market click crosses both accounts and cannot be taken back.
      ask('Cross both legs now?',
          key + '\n' + side + ' ' + (payload.quantity || pair.default_quantity) +
          ' spread(s) at ' + fmt(level, 4) + '.\n\n' +
          'MARKET mode crosses both legs immediately. The clicked price ' +
          'is the slippage guard: a fill worse than it by more than the ' +
          'protection is refused.',
          'Send it', function () { send('click', payload); });
      return;
    }
    send('click', payload);
  }

  function setPair(key, fields) {
    send('set_pair', {pair: key, fields: fields});
  }

  function renderLadder(key, row) {
    var node = ladderNode(key);
    var market = row.market || {};
    node.classList.toggle('mode-market', row.order_type === 'MARKET');
    node.classList.toggle('inactive', state.active !== panelId('ladder', key));
    node.querySelector('.title').textContent = row.name || key;
    node.querySelector('.route').textContent =
      (row.account_a || '?') + ' → ' + (row.account_b || '?');
    node.querySelector('.mode-badge').textContent =
      row.order_type + ' · ' + row.time_in_force;

    var change = market.net_change;
    var netchg = node.querySelector('.netchg');
    netchg.textContent = change === null || change === undefined
      ? DASH : (change > 0 ? '+' : '') + fmt(change, 4);
    netchg.classList.toggle('up', change > 0);

    var session = market.session || {};
    node.querySelector('.stat-h').textContent = fmt(session.high, 4);
    node.querySelector('.stat-l').textContent = fmt(session.low, 4);
    node.querySelector('.stat-o').textContent = fmt(session.open, 4);
    node.querySelector('.stat-v').textContent = fmt(session.volume, 2);

    setValue(node.querySelector('.order-type'), row.order_type);
    setValue(node.querySelector('.tif'), row.time_in_force);
    setValue(node.querySelector('.overnight'), row.overnight);
    setValue(node.querySelector('.increment'), row.increment);
    setValue(node.querySelector('.default-qty'), row.default_quantity);
    node.querySelector('.armed').textContent =
      state.armed[key] ? String(state.armed[key]) : '--';
    node.querySelector('.count-b').textContent = row.working_buys || '';
    node.querySelector('.count-s').textContent = row.working_sells || '';
    node.querySelector('.count-all').textContent =
      (row.working_buys + row.working_sells) || '';

    renderRows(node, key, row);

    var counts = node.querySelector('.counts');
    counts.querySelector('.cnt-b').textContent = 'B:' + bought(row);
    counts.querySelector('.cnt-s').textContent = 'S:' + sold(row);
    counts.querySelector('.cnt-w').textContent =
      'W:' + (row.working_buys + row.working_sells);

    var feed = node.querySelector('.feed');
    feed.textContent = market.feed_badge || DASH;
    feed.className = 'feed ' + badgeClass(market.feed_badge);
    node.querySelector('.pos').textContent = row.net_position
      ? (row.net_position > 0 ? '+' : '') + row.net_position + ' @ ' +
        fmt(row.avg_entry, 4)
      : 'flat';
    var pnl = node.querySelector('.pnl');
    pnl.textContent = row.open_pnl === null || row.open_pnl === undefined
      ? DASH : money(row.open_pnl);
    pnl.classList.toggle('up', row.open_pnl > 0);
    pnl.classList.toggle('down', row.open_pnl < 0);
    // The unit and the derivation, always (a number with no unit is not
    // checkable).
    node.querySelector('.clip').textContent =
      '1 spread = ' + fmt(row.clip_lots_a, 2) + ' A / ' +
      fmt(row.clip_lots_b, 2) + ' B, ' + money(row.spread_units) +
      ' per 1.00';
    node.querySelector('.errors').textContent = (row.errors || []).join(' ');
    return node;
  }

  function badgeClass(badge) {
    if (!badge) { return ''; }
    if (badge.indexOf('OK') === 0) { return 'ok'; }
    if (badge === 'warming up') { return 'warn'; }
    return 'bad';
  }

  function bought(row) {
    return (row.positions || []).filter(function (p) {
      return p.side === 'BUY';
    }).length;
  }

  function sold(row) {
    return (row.positions || []).filter(function (p) {
      return p.side === 'SELL';
    }).length;
  }

  function renderRows(node, key, row) {
    var body = node.querySelector('tbody');
    var rows = row.rows || [];
    var market = row.market || {};
    var lastPrint = row.last_print || {};
    var ordersByLevel = {};
    (row.orders || []).forEach(function (order) {
      var bucket = ordersByLevel[order.level.toFixed(6)] ||
        (ordersByLevel[order.level.toFixed(6)] = []);
      bucket.push(order);
    });

    var html = '';
    rows.forEach(function (line) {
      var level = line.level;
      var inBid = market.short_spread !== undefined &&
        level <= market.short_spread + 1e-12;
      var inAsk = market.long_spread !== undefined &&
        level >= market.long_spread - 1e-12;
      var orders = ordersByLevel[level.toFixed(6)] || [];
      if (state.filtered[key] && !orders.length && !line.is_best_bid &&
          !line.is_best_ask && !inBid && !inAsk) {
        return;
      }
      var work = orders.length ? orders[0] : null;
      var workQty = orders.reduce(function (sum, order) {
        return sum + (order.quantity - order.filled_quantity);
      }, 0);
      var classes = [];
      if (inBid) { classes.push('in-bid'); }
      if (inAsk) { classes.push('in-ask'); }
      if (line.is_best_bid) { classes.push('market-line'); }
      var isLast = lastPrint.level !== undefined &&
        Math.abs(lastPrint.level - level) < (row.increment || 1) / 2;

      html += '<tr class="' + classes.join(' ') + '" data-level="' + level + '">';
      html += '<td class="work' + (work ? ' ' + work.side.toLowerCase() : '') +
        '"' + (work ? ' data-order-id="' + work.order_id + '" title="' +
        orders.length + ' order(s) here — click to pull one"' : '') + '>' +
        (workQty ? workQty : '') + '</td>';
      // MT5 publishes no depth for a spread, so only the touch is real.
      html += '<td class="bid' + (line.is_best_bid ? ' has-qty' : '') +
        '" title="Click: BUY the spread at ' + fmt(level, 4) + '">' +
        (line.is_best_bid ? '▲' : '') + '</td>';
      html += '<td class="price' + (isLast ? ' last-trade' : '') + '">' +
        fmt(level, digitsFor(row.increment)) + '</td>';
      html += '<td class="ask' + (line.is_best_ask ? ' has-qty' : '') +
        '" title="Click: SELL the spread at ' + fmt(level, 4) + '">' +
        (line.is_best_ask ? '▼' : '') + '</td>';
      html += '<td class="ltq' + (isLast ? ' print' : '') + '">' +
        (isLast ? fmt(lastPrint.quantity, 2) : '') + '</td>';
      html += '</tr>';
    });
    body.innerHTML = html;

    if (!state.locked[key]) {
      // Centre BETWEEN the two touches, not on one of them: on a wide
      // book the other side is otherwise scrolled off the screen, and a
      // side you cannot see is a side you cannot trade. LOCKED, nothing
      // moves — a ladder that re-centres under a click is how a trader
      // clicks the wrong price.
      var bidRow = body.querySelector('tr.market-line');
      var askRows = body.querySelectorAll('tr.in-ask');
      var askRow = askRows.length ? askRows[askRows.length - 1] : null;
      var anchor = bidRow || askRow;
      if (anchor) {
        var middle = askRow && bidRow
          ? (askRow.offsetTop + bidRow.offsetTop + bidRow.offsetHeight) / 2
          : anchor.offsetTop;
        var grid = node.querySelector('.grid');
        grid.scrollTop = middle - grid.clientHeight / 2;
      }
    }
  }

  function digitsFor(increment) {
    if (!increment) { return 4; }
    var digits = Math.max(0, Math.ceil(-Math.log10(increment)));
    return Math.min(6, digits);
  }

  function setValue(input, value) {
    if (document.activeElement === input) { return; }   // do not fight typing
    var text = value === null || value === undefined ? '' : String(value);
    if (input.value !== text) { input.value = text; }
  }

  // -- the Market Grid --------------------------------------------------

  function renderGrid() {
    var node = document.querySelector('.market-grid');
    if (!node) {
      node = document.createElement('section');
      node.className = 'window market-grid';
      node.innerHTML =
        '<div class="titlebar"><span class="swatch"></span>' +
        '<span class="title">Market Grid</span>' +
        '<span class="winbtns"><button class="winbtn close">&times;</button></span></div>' +
        '<div class="grid"><table><thead></thead><tbody></tbody></table></div>';
      node.querySelector('.close').addEventListener('click', function () {
        closePanel(panelId('grid'));
      });
      node.querySelector('tbody').addEventListener('click', onGridClick);
      node.querySelector('tbody').addEventListener('change', onGridChange);
      el('desktop').appendChild(node);
    }
    node.querySelector('thead').innerHTML =
      '<tr><th>Contract</th><th>Bid</th><th>Ask</th><th>Last</th>' +
      '<th>Chg</th><th>Incr</th><th>Qty</th><th>k $</th><th>Net</th>' +
      '<th>Work</th><th>Avg</th><th>Open P&amp;L</th><th>Mode</th>' +
      '<th>TIF</th><th>O/N</th><th>Feed</th><th>&beta;</th><th></th></tr>';

    var html = '';
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      var market = row.market || {};
      var broken = (row.errors || []).length > 0;
      html += '<tr class="' + (broken ? 'broken' : '') + '" data-pair="' +
        key + '">';
      html += '<td class="contract" title="Open this ladder">' +
        (row.name || key) + '</td>';
      // Bid = the SHORT spread (where you can sell it); Ask = the LONG.
      html += '<td class="bid" title="Click: SELL the spread here">' +
        fmt(row.short_spread, digitsFor(row.increment)) + '</td>';
      html += '<td class="ask" title="Click: BUY the spread here">' +
        fmt(row.long_spread, digitsFor(row.increment)) + '</td>';
      html += '<td>' + fmt((row.last_print || {}).level,
                           digitsFor(row.increment)) + '</td>';
      html += '<td>' + fmt(market.net_change, 4) + '</td>';
      html += '<td><input class="incr" type="number" step="0.0001" value="' +
        (row.increment === null ? '' : row.increment) + '" title="' +
        'Derived: max(tick B, beta x tick A) = ' +
        fmt(row.increment_derived, 6) + '"></td>';
      html += '<td><input class="qty" type="number" step="0.01" value="' +
        row.default_quantity + '"></td>';
      html += '<td>' + money(row.spread_units) + '</td>';
      html += '<td>' + (row.net_position || 0) + '</td>';
      html += '<td>' + row.working_buys + '/' + row.working_sells + '</td>';
      html += '<td>' + fmt(row.avg_entry, 4) + '</td>';
      html += '<td>' + (row.open_pnl === null || row.open_pnl === undefined
                        ? DASH : money(row.open_pnl)) + '</td>';
      html += '<td>' + select('mode', ['LIMIT', 'MARKET'], row.order_type) + '</td>';
      html += '<td>' + select('tif', ['DAY', 'GTC'], row.time_in_force) + '</td>';
      html += '<td>' + select('on', ['ALLOW', 'EXIT_IF_PROFIT', 'EXIT_ALWAYS'],
                              row.overnight) + '</td>';
      html += '<td>' + (market.feed_badge || DASH) + '</td>';
      html += '<td title="Stamped for ' + (row.hedge_ratio_for || '?') + '">' +
        fmt(row.hedge_ratio, 4) + '</td>';
      html += '<td class="reason">' + (row.errors || []).join(' ') + '</td>';
      html += '</tr>';
    });
    node.querySelector('tbody').innerHTML = html;
    node.classList.toggle('inactive', state.active !== panelId('grid'));
    return node;
  }

  function select(name, options, value) {
    var html = '<select class="' + name + '">';
    options.forEach(function (option) {
      html += '<option value="' + option + '"' +
        (option === value ? ' selected' : '') + '>' + option + '</option>';
    });
    return html + '</select>';
  }

  function onGridClick(e) {
    var cell = e.target.closest('td');
    if (!cell) { return; }
    var key = cell.closest('tr').dataset.pair;
    var row = state.snapshot.pairs[key] || {};
    if (cell.classList.contains('contract')) {
      openPanel(panelId('ladder', key));
    } else if (cell.classList.contains('bid')) {
      // Identical semantics to a ladder click, through the identical
      // code path — a test asserts it.
      clickLevel(key, 'SELL', row.short_spread);
    } else if (cell.classList.contains('ask')) {
      clickLevel(key, 'BUY', row.long_spread);
    }
  }

  function onGridChange(e) {
    var input = e.target;
    var key = input.closest('tr').dataset.pair;
    if (input.classList.contains('incr')) {
      setPair(key, {increment: parseFloat(input.value)});
    } else if (input.classList.contains('qty')) {
      setPair(key, {default_quantity: parseFloat(input.value)});
    } else if (input.classList.contains('mode')) {
      setPair(key, {order_type: input.value});
    } else if (input.classList.contains('tif')) {
      setPair(key, {time_in_force: input.value});
    } else if (input.classList.contains('on')) {
      setPair(key, {overnight: input.value});
    }
  }

  // -- the positions monitor ---------------------------------------------

  function renderMonitor() {
    var node = document.querySelector('.monitor');
    if (!node) {
      node = document.createElement('section');
      node.className = 'window monitor';
      node.innerHTML =
        '<div class="titlebar"><span class="swatch"></span>' +
        '<span class="title">Positions</span>' +
        '<span class="winbtns"><button class="winbtn close">&times;</button></span></div>' +
        '<div class="tabs">' +
        '<button data-tab="positions">Positions</button>' +
        '<button data-tab="orders">Working Orders</button>' +
        '<button data-tab="fills">Fills</button>' +
        '<button data-tab="reconcile">Reconciler</button></div>' +
        '<div class="pane"></div><div class="note"></div>';
      node.querySelector('.close').addEventListener('click', function () {
        closePanel(panelId('monitor'));
      });
      node.querySelector('.tabs').addEventListener('click', function (e) {
        var button = e.target.closest('button');
        if (!button) { return; }
        state.monitorTab = button.dataset.tab;
        render();
      });
      node.querySelector('.pane').addEventListener('click', onMonitorClick);
      el('desktop').appendChild(node);
    }
    Array.prototype.forEach.call(node.querySelectorAll('.tabs button'),
      function (button) {
        button.classList.toggle('on', button.dataset.tab === state.monitorTab);
      });
    var pane = node.querySelector('.pane');
    if (state.monitorTab === 'positions') { pane.innerHTML = positionsTable(); }
    else if (state.monitorTab === 'orders') { pane.innerHTML = ordersTable(); }
    else if (state.monitorTab === 'fills') { pane.innerHTML = fillsTable(); }
    else { pane.innerHTML = reconcileTable(); }
    node.querySelector('.note').textContent =
      'Marked at the touches these would actually CLOSE at, less ' +
      'commission only — so a position shows a loss the instant it ' +
      'opens, equal to one round turn of both legs’ bid-ask. That is ' +
      'what closing it immediately would cost.';
    node.classList.toggle('inactive', state.active !== panelId('monitor'));
    return node;
  }

  function eachPosition(callback) {
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      (state.snapshot.pairs[key].positions || []).forEach(function (position) {
        callback(key, state.snapshot.pairs[key], position);
      });
    });
  }

  function positionsTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Net</th>' +
      '<th>Avg entry</th><th>Mark</th><th>Open P&amp;L</th><th>Mode</th>' +
      '<th>Slip</th><th>Click→on</th><th>Legs</th><th></th></tr></thead><tbody>';
    var total = 0;
    var any = false;
    eachPosition(function (key, row, position) {
      any = true;
      if (position.net_pnl !== null && position.net_pnl !== undefined) {
        total += position.net_pnl;
      }
      html += '<tr data-position="' + position.position_id + '">';
      html += '<td>' + (row.name || key) + '</td>';
      html += '<td>' + position.side + '</td>';
      html += '<td>' + position.quantity + '</td>';
      html += '<td>' + fmt(position.entry_spread, 4) + '</td>';
      html += '<td>' + fmt(position.closing_spread, 4) + '</td>';
      html += '<td class="' + (position.net_pnl > 0 ? 'up' : 'down') + '">' +
        money(position.net_pnl) + '</td>';
      html += '<td>' + position.order_type + '</td>';
      html += '<td>' + fmt(position.entry_slippage, 4) + '</td>';
      html += '<td>' + (position.click_to_on_ms === null ||
                        position.click_to_on_ms === undefined
                        ? DASH : Math.round(position.click_to_on_ms) + 'ms') +
        '</td>';
      html += '<td>' + legText(position.leg_a) + ' / ' +
        legText(position.leg_b) + '</td>';
      html += '<td><button class="btn close-position">Flatten</button></td>';
      html += '</tr>';
      html += '<tr class="detail"><td colspan="11">' +
        legDetail(position.leg_a, 'A') + ' &nbsp; ' +
        legDetail(position.leg_b, 'B') + '</td></tr>';
    });
    if (!any) { html += '<tr><td colspan="11">nothing open</td></tr>'; }
    html += '</tbody></table>';
    html += accountReconciliation(total);
    return html;
  }

  function legText(leg) {
    if (!leg) { return DASH; }
    return leg.side + ' ' + leg.volume + ' @ ' + fmt(leg.price, 2);
  }

  function legDetail(leg, label) {
    if (!leg) { return 'leg ' + label + ': ' + DASH; }
    return 'leg ' + label + ' ' + leg.symbol + ' on ' + leg.account +
      ' · tickets ' + (leg.position_tickets.join(', ') || DASH) +
      ' · ' + leg.volume + ' lots × ' +
      (leg.contract_size || DASH) + '/lot';
  }

  function accountReconciliation(ourTotal) {
    // Our total against MT5's OWN per-account profit. A difference is a
    // fault to be SHOWN, not smoothed.
    var accounts = state.snapshot.accounts || {};
    var theirs = 0;
    var known = false;
    Object.keys(accounts).forEach(function (name) {
      var info = accounts[name];
      if (info && typeof info.profit === 'number') {
        theirs += info.profit;
        known = true;
      }
    });
    if (!known) {
      return '<div class="note">MT5’s own profit could not be read, ' +
        'so there is nothing to reconcile against — unmeasured, not zero.</div>';
    }
    var difference = ourTotal - theirs;
    var bad = Math.abs(difference) > 0.01;
    return '<table><tbody><tr' + (bad ? ' class="mismatch"' : '') +
      '><td>our total</td><td>' + money(ourTotal) +
      '</td><td>MT5’s own</td><td>' + money(theirs) +
      '</td><td>difference</td><td>' + money(difference) +
      '</td></tr></tbody></table>';
  }

  function ordersTable() {
    var html = '<table><thead><tr><th>Pair</th><th>Side</th><th>Level</th>' +
      '<th>Qty</th><th>TIF</th><th>State</th><th>Pending</th>' +
      '<th>Peg</th><th>Implied</th><th>Re-pegs</th><th>Why</th><th></th>' +
      '</tr></thead><tbody>';
    var any = false;
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      var quotes = {};
      (row.quotes || []).forEach(function (quote) {
        quote.orders.forEach(function (id) { quotes[id] = quote; });
      });
      (row.orders || []).forEach(function (order) {
        any = true;
        var quote = quotes[order.order_id] || {};
        html += '<tr data-order="' + order.order_id + '">';
        html += '<td>' + (row.name || key) + '</td>';
        html += '<td>' + order.side + '</td>';
        html += '<td>' + fmt(order.level, 4) + '</td>';
        html += '<td>' + (order.quantity - order.filled_quantity) + '</td>';
        html += '<td>' + order.time_in_force + '</td>';
        html += '<td>' + order.state + '</td>';
        html += '<td>' + (quote.ticket || DASH) + '</td>';
        html += '<td>' + fmt(quote.price, 2) + '</td>';
        html += '<td>' + (quote.leg ? 'leg ' + quote.leg : DASH) + '</td>';
        html += '<td>' + (quote.repegs === undefined ? DASH : quote.repegs) +
          '</td>';
        html += '<td>' + (quote.reason || order.reason || '') + '</td>';
        html += '<td><button class="btn cancel-order">Cancel</button></td>';
        html += '</tr>';
      });
    });
    if (!any) { html += '<tr><td colspan="12">nothing working</td></tr>'; }
    html += '</tbody></table>';
    html += '<div class="note">GTC here means: until cancelled, or until ' +
      'this system stops. A synthetic order lives in the coordinator, and ' +
      'nothing watches the spread while it is down — so neither DAY ' +
      'nor GTC survives a restart. The real pending behind a LIMIT order ' +
      'DOES survive at the broker, which is why it is swept at shutdown ' +
      'and again at startup.</div>';
    return html;
  }

  function fillsTable() {
    var snapshot = state.snapshot;
    var html = '<table><thead><tr><th>What</th><th>Value</th></tr></thead><tbody>';
    html += timingRow('fill → hedge on (LIMIT)', snapshot.hedge_times_ms,
                      'against the 2.0s escalation deadline');
    html += timingRow('click → both legs on (MARKET)',
                      snapshot.click_to_on_ms, 'measured, not assumed');
    html += '</tbody></table>';
    html += '<table><thead><tr><th>Pair</th><th>Side</th><th>Qty</th>' +
      '<th>Entry</th><th>Exit</th><th>Mode</th><th>Slip in</th>' +
      '<th>Slip out</th><th>Net</th></tr></thead><tbody>';
    var any = false;
    Object.keys(snapshot.pairs || {}).forEach(function (key) {
      (snapshot.pairs[key].positions || []).forEach(function (position) {
        any = true;
        html += '<tr><td>' + key + '</td><td>' + position.side + '</td><td>' +
          position.quantity + '</td><td>' + fmt(position.entry_spread, 4) +
          '</td><td>' + fmt(position.exit_spread, 4) + '</td><td>' +
          position.order_type + '</td><td>' +
          fmt(position.entry_slippage, 4) + '</td><td>' +
          fmt(position.exit_slippage, 4) + '</td><td>' +
          (position.realized_pnl === null || position.realized_pnl === undefined
           ? DASH : money(position.realized_pnl)) + '</td></tr>';
      });
    });
    if (!any) { html += '<tr><td colspan="9">no fills yet</td></tr>'; }
    return html + '</tbody></table>';
  }

  function timingRow(label, values, note) {
    if (!values || !values.length) {
      return '<tr><td>' + label + '</td><td>' + DASH +
        ' <span class="note">nothing measured yet — not zero</span></td></tr>';
    }
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var worst = sorted[sorted.length - 1];
    var median = sorted[Math.floor(sorted.length / 2)];
    return '<tr><td>' + label + '</td><td>median ' + Math.round(median) +
      'ms, worst ' + Math.round(worst) + 'ms over ' + values.length +
      ' — ' + note + '</td></tr>';
  }

  function reconcileTable() {
    var reconciler = state.snapshot.reconciler || {};
    var html = '<table><thead><tr><th>When</th><th>Symbol</th><th>Ticket</th>' +
      '<th>Volume</th><th>Contract</th><th>P&amp;L</th><th>Note</th>' +
      '</tr></thead><tbody>';
    var closes = reconciler.untracked_closes || [];
    closes.forEach(function (entry) {
      html += '<tr><td>' + new Date(entry.at * 1000).toLocaleTimeString() +
        '</td><td>' + entry.symbol + '</td><td>' + entry.ticket + '</td><td>' +
        entry.volume + '</td><td>' + entry.contract_size +
        (entry.contract_size_assumed ? ' (assumed)' : '') + '</td><td>' +
        money(entry.pnl) + '</td><td>' + (entry.note || '') + '</td></tr>';
    });
    if (!closes.length) {
      html += '<tr><td colspan="7">nothing untracked has been closed</td></tr>';
    }
    html += '</tbody></table>';
    if ((reconciler.escalated || []).length) {
      html += '<div class="note mismatch">CLOSE IT BY HAND: ' +
        reconciler.escalated.join(', ') + '</div>';
    }
    if ((reconciler.unknown_accounts || []).length) {
      html += '<div class="note">' + reconciler.unknown_accounts.join(', ') +
        ' could not be read this pass — UNKNOWN, not flat. No orphans ' +
        'or ghosts were inferred from them.</div>';
    }
    return html;
  }

  function onMonitorClick(e) {
    var button = e.target.closest('button');
    if (!button) { return; }
    var row = button.closest('tr');
    if (button.classList.contains('close-position')) {
      send('close_position', {position_id: row.dataset.position});
    } else if (button.classList.contains('cancel-order')) {
      send('cancel_order', {order_id: row.dataset.order});
    }
  }

  // -- the whole screen ---------------------------------------------------

  function render() {
    var snapshot = state.snapshot;
    var banner = el('engine-banner');
    if (snapshot.engine && snapshot.engine !== 'up') {
      banner.textContent = snapshot.engine_note;
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }

    renderNaked();

    var wanted = {};
    state.open.forEach(function (id) { wanted[id] = true; });
    Object.keys(snapshot.pairs || {}).forEach(function (key) {
      var id = panelId('ladder', key);
      if (wanted[id]) { renderLadder(key, snapshot.pairs[key]); }
    });
    if (wanted[panelId('grid')]) { renderGrid(); }
    if (wanted[panelId('monitor')]) { renderMonitor(); }

    // Remove the panels that are no longer open.
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        var id = node.classList.contains('market-grid') ? panelId('grid')
          : node.classList.contains('monitor') ? panelId('monitor')
            : panelId('ladder', node.dataset.pair);
        if (!wanted[id]) { node.remove(); }
      });

    renderTabs();
    var loop = snapshot.loop_interval_sec;
    el('loop-stat').textContent = loop
      ? 'loop ' + (loop * 1000).toFixed(0) + 'ms · snapshot ' +
        ((snapshot.status_age_sec || 0) * 1000).toFixed(0) + 'ms old'
      : DASH;
  }

  function renderNaked() {
    var messages = [];
    Object.keys(state.snapshot.pairs || {}).forEach(function (key) {
      var row = state.snapshot.pairs[key];
      (row.positions || []).forEach(function (position) {
        var a = position.leg_a, b = position.leg_b;
        if (!a || !b || !a.volume || !b.volume) {
          messages.push(key + ': one leg is on and the other is not');
        }
      });
    });
    var banner = el('naked-banner');
    if (messages.length) {
      banner.textContent = 'NAKED LEG — ' + messages.join(' | ') +
        '. Hedge it or flatten it now.';
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }
  }

  function renderTabs() {
    var html = '';
    state.open.forEach(function (id) {
      var parts = id.split(':');
      var label = parts[0] === 'ladder'
        ? ((state.snapshot.pairs[parts.slice(1).join(':')] || {}).name ||
           parts.slice(1).join(':'))
        : (parts[0] === 'grid' ? 'Market Grid' : 'Positions');
      var badge = '';
      if (parts[0] === 'ladder') {
        var row = state.snapshot.pairs[parts.slice(1).join(':')] || {};
        var working = (row.working_buys || 0) + (row.working_sells || 0);
        if (working) { badge = '<span class="badge">' + working + '</span>'; }
      }
      html += '<button class="tab' + (state.active === id ? ' on' : '') +
        '" data-panel="' + id + '">' + label + badge + '</button>';
    });
    el('tabs').innerHTML = html;
  }

  // -- polling -------------------------------------------------------------

  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(function (snapshot) {
        state.snapshot = snapshot;
        if (!state.open.length) {
          // First load: a ladder for every enabled pair, plus the grid.
          Object.keys(snapshot.pairs || {}).forEach(function (key) {
            if (snapshot.pairs[key].enabled) {
              state.open.push(panelId('ladder', key));
            }
          });
          state.open.push(panelId('grid'));
          state.open.push(panelId('monitor'));
          state.active = state.open[0];
        }
        render();
      })
      .catch(function (error) {
        el('engine-banner').textContent =
          'the web process is not answering: ' + error.message;
        el('engine-banner').classList.remove('hidden');
      });
  }

  function start() {
    el('add-panel').addEventListener('click', function () {
      openPanel(panelId('grid'));
    });
    el('tabs').addEventListener('click', function (e) {
      var button = e.target.closest('.tab');
      if (button) { state.active = button.dataset.panel; render(); }
    });
    el('kill').addEventListener('click', function () {
      ask('Cancel everything, on every ladder?',
          'This pulls every working order across every pair. Tick nothing ' +
          'and it stops there; the second button also FLATTENS every open ' +
          'position at market, by ticket, which cannot be undone.',
          'Cancel all + flatten', function () {
            send('kill', {flatten: true});
          });
    });
    el('modal-cancel').addEventListener('click', closeModal);
    el('modal-confirm').addEventListener('click', function () {
      var handler = el('modal')._onConfirm;
      closeModal();
      if (handler) { handler(); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeModal(); }
    });
    window.addEventListener('click', function (e) {
      var window_ = e.target.closest('.window');
      if (!window_) { return; }
      state.active = window_.classList.contains('market-grid') ? panelId('grid')
        : window_.classList.contains('monitor') ? panelId('monitor')
          : panelId('ladder', window_.dataset.pair);
    });
    poll();
    window.setInterval(poll, REFRESH_MS);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }

  window.MT5Trader = {state: state, render: render};   // for the UI tests
})();
