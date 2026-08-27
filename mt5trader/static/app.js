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
    monitorTab: 'positions',
    // Clicks that have been sent but are not in a snapshot yet. The
    // round trip is ~25ms plus the broker, but the ladder must show the
    // order the INSTANT it is clicked: a trader who cannot see their
    // click clicks again.
    pending: [],
    help: false,
    // The journal, from the database. Not in the snapshot: it is the
    // BROKER's record, it outlives the process, and it is read on its
    // own slow clock rather than three times a second.
    fills: null,
    fillsAt: 0,
    fillsFilter: {ours: false}
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
    // Grouped: an account equity of 100000.00 is a number nobody reads
    // at a glance, and this column is read at a glance.
    return sign + '$' + Math.abs(value).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2});
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
    node.querySelector('.buy-touch').addEventListener('click', function () {
      atTouch(key, 'BUY');
    });
    node.querySelector('.sell-touch').addEventListener('click', function () {
      atTouch(key, 'SELL');
    });
    node.querySelector('.flatten').addEventListener('click', function () {
      flatten(key);
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
    var quantity = state.armed[key] || pair.default_quantity;
    var payload = {pair: key, side: side, level: level};
    if (state.armed[key]) { payload.quantity = state.armed[key]; }

    // ONE CLICK IS ONE ORDER. A market click crosses both accounts
    // immediately — that is the product. The arming is what carries the
    // weight instead: the mode badge, the tinted columns, the cursor.
    // A desk that wants the extra gesture turns CONFIRM_MARKET_CLICKS on.
    if (pair.order_type === 'MARKET' && state.snapshot.confirm_market_clicks) {
      ask('Cross both legs now?',
          key + '\n' + side + ' ' + quantity + ' spread(s) at ' +
          fmt(level, 4) + '.\n\n' +
          'MARKET crosses both legs immediately. The clicked price is the ' +
          'slippage guard: a fill worse than it by more than the ' +
          'protection is refused.',
          'Send it', function () { fire(key, side, level, payload); });
      return;
    }
    fire(key, side, level, payload);
  }

  function fire(key, side, level, payload) {
    var pair = state.snapshot.pairs[key] || {};
    var ghost = {
      pair_key: key, side: side, level: level,
      quantity: payload.quantity || pair.default_quantity,
      market: pair.order_type === 'MARKET', at: Date.now()
    };
    state.pending.push(ghost);
    flash(key, level, side);
    render();
    send('click', payload, function (result) {
      ghost.done = true;
      if (result && result.ok === false) { ghost.failed = true; }
      render();
    });
  }

  function flash(key, level, side) {
    // The row lights up under the finger, before any round trip. It is
    // the difference between "did that register?" and knowing it did.
    var selector = '.ladder[data-pair="' + cssEscape(key) + '"] tr[data-level="'
      + level + '"]';
    var row = document.querySelector(selector);
    if (!row) { return; }
    row.classList.add(side === 'BUY' ? 'flash-buy' : 'flash-sell');
    window.setTimeout(function () {
      row.classList.remove('flash-buy');
      row.classList.remove('flash-sell');
    }, 350);
  }

  function prunePending() {
    var now = Date.now();
    state.pending = state.pending.filter(function (ghost) {
      // A ghost lives until the engine's own answer arrives, and never
      // more than a second: a stale ghost is a phantom order.
      if (ghost.failed) { return false; }
      if (ghost.done && now - ghost.at > 400) { return false; }
      return now - ghost.at < 1500;
    });
  }

  function pendingAt(key, level, increment) {
    var half = (increment || 0.0001) / 2;
    return state.pending.filter(function (ghost) {
      return ghost.pair_key === key && !ghost.failed &&
        Math.abs(ghost.level - level) < half;
    });
  }

  function setPair(key, fields) {
    send('set_pair', {pair: key, fields: fields});
  }

  function atTouch(key, side) {
    // Hit the touch without hunting for the row: the fastest
    // possible "I want in, now" on the ladder already in front of you.
    var row = state.snapshot.pairs[key];
    if (!row) { return; }
    // BUY lifts the long spread, SELL hits the short one — the price the
    // market is actually offering for that direction, never the mid.
    var level = side === 'BUY' ? row.long_spread : row.short_spread;
    if (level === null || level === undefined) {
      toast(key + ' has no price yet — nothing to hit');
      return;
    }
    clickLevel(key, side, level);
  }

  function flatten(key) {
    var row = state.snapshot.pairs[key] || {};
    if (!row.net_position) {
      toast((row.name || key) + ' is already flat');
      return;
    }
    // Flattening is irreversible and it is the button pressed in a
    // hurry, so it asks — but with one key, and only once.
    ask('Flatten ' + (row.name || key) + '?',
        (row.net_position > 0 ? '+' : '') + row.net_position +
        ' spreads, at market, by ticket. This cannot be undone.',
        'Flatten now', function () { send('flatten_pair', {pair: key}); });
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
    if (state.snapshot.row_height_px) {
      // A bigger target is a faster and safer click. 17px is the
      // reference screen's; a large monitor wants more.
      node.style.setProperty('--row-h',
                             state.snapshot.row_height_px + 'px');
    }
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
      // Clicks that are on their way. Shown as their own thing, never
      // added into the working total: a ghost is not an order yet.
      var ghosts = pendingAt(key, level, row.increment);
      var ghostQty = ghosts.reduce(function (sum, ghost) {
        return sum + ghost.quantity;
      }, 0);
      var classes = [];
      if (inBid) { classes.push('in-bid'); }
      if (inAsk) { classes.push('in-ask'); }
      if (line.is_best_bid) { classes.push('market-line'); }
      var isLast = lastPrint.level !== undefined &&
        Math.abs(lastPrint.level - level) < (row.increment || 1) / 2;

      html += '<tr class="' + classes.join(' ') + '" data-level="' + level + '">';
      var ghostSide = ghosts.length ? ghosts[0].side.toLowerCase() : '';
      html += '<td class="work' +
        (work ? ' ' + work.side.toLowerCase() : '') +
        (ghosts.length ? ' pending ' + ghostSide : '') + '"' +
        (work ? ' data-order-id="' + work.order_id + '" title="' +
          orders.length + ' order(s) here — click to pull one"' : '') + '>' +
        (workQty ? workQty : '') +
        (ghostQty ? '<span class="ghost" title="sent — waiting for the ' +
          'engine">' + ghostQty + '</span>' : '') + '</td>';
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
        '<button data-tab="accounts">Accounts</button>' +
        '<button data-tab="reconcile">Reconciler</button></div>' +
        // `monitor-note`, not `note`: the panes have notes of their own,
        // and a bare `.note` selector reaches into them and overwrites
        // the first one it finds.
        '<div class="pane"></div><div class="note monitor-note"></div>';
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
      node.querySelector('.pane').addEventListener('change', function (e) {
        if (e.target.classList.contains('ours-only')) {
          state.fillsFilter.ours = e.target.checked;
          loadFills(true);
        }
      });
      el('desktop').appendChild(node);
    }
    Array.prototype.forEach.call(node.querySelectorAll('.tabs button'),
      function (button) {
        button.classList.toggle('on', button.dataset.tab === state.monitorTab);
      });
    var pane = node.querySelector('.pane');
    if (state.monitorTab === 'positions') { pane.innerHTML = positionsTable(); }
    else if (state.monitorTab === 'orders') { pane.innerHTML = ordersTable(); }
    else if (state.monitorTab === 'fills') {
      loadFills();
      pane.innerHTML = fillsTable();
    }
    else if (state.monitorTab === 'accounts') {
      pane.innerHTML = accountsTable();
    }
    else { pane.innerHTML = reconcileTable(); }
    node.querySelector('.monitor-note').textContent =
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
    var html = '<table><thead><tr><th>What</th><th>Value</th></tr></thead>' +
      '<tbody>';
    html += timingRow('fill → hedge on (LIMIT)', snapshot.hedge_times_ms,
                      'against the 2.0s escalation deadline');
    html += timingRow('click → both legs on (MARKET)',
                      snapshot.click_to_on_ms, 'measured, not assumed');
    html += '</tbody></table>';

    var journal = state.fills;
    html += '<div class="journal-controls">' +
      '<label class="check"><input type="checkbox" class="ours-only"' +
      (state.fillsFilter.ours ? ' checked' : '') + '> ours only</label>' +
      '<a class="btn" href="/api/fills.csv" download>Export CSV</a>' +
      '<span class="hint">Read back from MT5\'s own deal history — so it ' +
      'carries the trader\'s terminal clicks too, marked as not ours.' +
      '</span></div>';

    if (journal === null) {
      return html + '<p class="note">loading the journal…</p>';
    }
    if (journal.error) {
      return html + '<p class="note mismatch">' + journal.error + '</p>';
    }

    var totals = journal.totals || {};
    html += '<table><tbody><tr><td>' + (totals.fills || 0) + ' fills</td>' +
      '<td>' + fmt(totals.volume, 2) + ' lots</td>' +
      '<td>commission ' + money(totals.commission) + '</td>' +
      '<td>swap ' + money(totals.swap) + '</td>' +
      '<td>the broker\'s own P&amp;L ' + money(totals.profit) +
      '</td></tr></tbody></table>';

    html += '<table><thead><tr><th>Broker time</th><th>Account</th>' +
      '<th>Symbol</th><th>Pair</th><th>Leg</th><th>Side</th><th>In/Out</th>' +
      '<th>Volume</th><th>Price</th><th>Comm</th><th>Swap</th>' +
      '<th>P&amp;L</th><th>Ticket</th><th>Deal</th><th>Ours</th>' +
      '<th>Comment</th></tr></thead><tbody>';
    (journal.fills || []).forEach(function (fill) {
      html += '<tr class="' + (fill.is_ours ? '' : 'theirs') + '">';
      html += '<td>' + brokerTime(fill) + '</td>';
      html += '<td>' + (fill.account || DASH) + '</td>';
      html += '<td>' + (fill.symbol || DASH) + '</td>';
      html += '<td>' + (fill.pair_key || DASH) + '</td>';
      html += '<td>' + (fill.leg || DASH) + '</td>';
      html += '<td>' + (fill.side || DASH) + '</td>';
      html += '<td>' + (fill.entry || DASH) + '</td>';
      html += '<td>' + fmt(fill.volume, 2) + '</td>';
      html += '<td>' + fmt(fill.price, 4) + '</td>';
      html += '<td>' + money(fill.commission) + '</td>';
      html += '<td>' + money(fill.swap) + '</td>';
      html += '<td>' + money(fill.profit) + '</td>';
      html += '<td>' + (fill.position_ticket || DASH) + '</td>';
      html += '<td>' + (fill.deal_id || DASH) + '</td>';
      html += '<td>' + (fill.is_ours ? 'yes' : 'no') + '</td>';
      html += '<td>' + (fill.comment || '') + '</td>';
      html += '</tr>';
    });
    if (!(journal.fills || []).length) {
      html += '<tr><td colspan="16">no fills recorded yet</td></tr>';
    }
    return html + '</tbody></table>';
  }

  function brokerTime(fill) {
    // The BROKER's wall clock, which is what MT5's own History shows.
    // Rendering it in the browser's zone would put every row hours away
    // from the same trade in the terminal.
    if (!fill.broker_time_ms) { return DASH; }
    var stamp = new Date(fill.broker_time_ms);
    return stamp.toISOString().slice(11, 19) +
      (fill.server_offset_s === null || fill.server_offset_s === undefined
        ? '' : ' (broker)');
  }

  function loadFills(force) {
    var now = Date.now();
    if (!force && state.fills !== null && now - state.fillsAt < 5000) {
      return;
    }
    state.fillsAt = now;
    var query = state.fillsFilter.ours ? '?ours=1' : '';
    fetch('/api/fills' + query).then(function (r) { return r.json(); })
      .then(function (body) {
        state.fills = body.ok ? body : {error: body.error};
        render();
      })
      .catch(function (error) {
        state.fills = {error: 'the journal could not be read: ' +
                              error.message};
      });
  }

  function timingRow(label, values, note) {
    if (!values || !values.length) {
      // `hint`, not `note`: the note style is a full-width banner, and
      // inside a table cell it paints a yellow band across the table.
      return '<tr><td>' + label + '</td><td>' + DASH +
        ' <span class="hint">nothing measured yet — not zero</span>' +
        '</td></tr>';
    }
    var sorted = values.slice().sort(function (a, b) { return a - b; });
    var worst = sorted[sorted.length - 1];
    var median = sorted[Math.floor(sorted.length / 2)];
    return '<tr><td>' + label + '</td><td>median ' + Math.round(median) +
      'ms, worst ' + Math.round(worst) + 'ms over ' + values.length +
      ' — ' + note + '</td></tr>';
  }

  function accountsTable() {
    // Margin is posted PER ACCOUNT with two brokers. There is no
    // combined figure worth showing: the pair can only be carried by
    // the WEAKER of the two, and a total would read comfortable while
    // one side sits at its stop-out.
    var health = state.snapshot.account_health || {};
    var rows = health.accounts || {};
    var names = Object.keys(rows);
    var html = '';

    if (health.weakest) {
      var weakest = rows[health.weakest] || {};
      html += '<div class="note' + (weakest.tight ? ' mismatch' : '') + '">' +
        'The weakest account governs: <b>' + health.weakest + '</b> at ' +
        fmt(health.weakest_level, 1) + '% margin level' +
        (weakest.tight
          ? ' — under the ' + fmt(health.warn_level, 0) + '% you set, and ' +
            'it is this account that stops the pair, not the pair\'s total.'
          : '.') + '</div>';
    }
    if ((health.unknown || []).length) {
      html += '<div class="note mismatch">' + health.unknown.join(', ') +
        ' could not be read — UNKNOWN, not flat and not funded.</div>';
    }

    html += '<table><thead><tr><th>Account</th><th>Login</th>' +
      '<th>Equity</th><th>Balance</th><th>Credit</th><th>Open P&amp;L</th>' +
      '<th>Margin used</th><th>Free</th><th>Level</th><th>Call / Stop</th>' +
      '<th>Leverage</th><th>Our legs</th><th>Our lots</th><th>Our units</th>' +
      '</tr></thead><tbody>';
    names.forEach(function (name) {
      var row = rows[name];
      html += '<tr class="' + (row.tight ? 'mismatch' : '') + '">';
      html += '<td>' + name + '</td>';
      html += '<td>' + (row.login || DASH) +
        (row.server ? '<div class="hint">' + row.server + '</div>' : '') +
        '</td>';
      // Equity first, then balance and credit beside it: a demo funded
      // with credit shows a balance of 0.00 against real equity.
      html += '<td>' + money(row.equity) + '</td>';
      html += '<td>' + money(row.balance) + '</td>';
      html += '<td>' + money(row.credit) + '</td>';
      html += '<td class="' + (row.profit > 0 ? 'up' : row.profit < 0
                               ? 'down' : '') + '">' + money(row.profit) +
        '</td>';
      html += '<td>' + money(row.margin) + '</td>';
      html += '<td>' + money(row.margin_free) + '</td>';
      html += '<td>' + (row.margin_level === null ||
                        row.margin_level === undefined
                        ? DASH : fmt(row.margin_level, 1) + '%') + '</td>';
      html += '<td>' + (row.so_call === null || row.so_call === undefined
                        ? DASH : fmt(row.so_call, 0) + '% / ' +
                          fmt(row.so_so, 0) + '%') + '</td>';
      html += '<td>' + (row.leverage ? row.leverage + 'x' : DASH) + '</td>';
      html += '<td>' + row.our_legs + '</td>';
      html += '<td>' + fmt(row.our_lots, 2) + '</td>';
      html += '<td>' + fmt(row.our_units, 2) + '</td>';
      html += '</tr>';
    });
    if (!names.length) {
      html += '<tr><td colspan="14">no accounts connected</td></tr>';
    }
    html += '</tbody></table>';
    html += '<div class="note">Equity is what the broker actually has ' +
      'of yours: balance plus credit plus open P&amp;L. Brokers often ' +
      'fund a demo with CREDIT, so a balance of 0.00 against real ' +
      'equity is normal rather than alarming. "Our lots" and "our ' +
      'units" are this system\'s own legs on that account — what is ' +
      'left is somebody else\'s, or the trader\'s own.</div>';
    return html;
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
    prunePending();
    var banner = el('engine-banner');
    if (snapshot.engine && snapshot.engine !== 'up') {
      banner.textContent = snapshot.engine_note;
      banner.classList.remove('hidden');
    } else {
      banner.classList.add('hidden');
    }

    renderNaked();
    renderUnclaimed();

    var wanted = {};
    state.open.forEach(function (id) { wanted[id] = true; });
    Object.keys(snapshot.pairs || {}).forEach(function (key) {
      var id = panelId('ladder', key);
      if (wanted[id]) { renderLadder(key, snapshot.pairs[key]); }
    });
    if (wanted[panelId('grid')]) { renderGrid(); }
    if (wanted[panelId('monitor')]) { renderMonitor(); }
    if (wanted[panelId('settings')] && window.MT5Settings) {
      window.MT5Settings.render();
    }

    // Remove the panels that are no longer open.
    Array.prototype.forEach.call(document.querySelectorAll('.window'),
      function (node) {
        var id = node.classList.contains('market-grid') ? panelId('grid')
          : node.classList.contains('monitor') ? panelId('monitor')
            : node.classList.contains('settings') ? panelId('settings')
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

  function renderUnclaimed() {
    var reconciler = state.snapshot.reconciler || {};
    var unclaimed = reconciler.unclaimed || [];
    var banner = el('unclaimed-banner');
    if (!unclaimed.length) {
      banner.classList.add('hidden');
      return;
    }
    // A position we cannot explain is exactly the one an automatic close
    // must not touch. It sits here until a person decides.
    var html = '<b>' + unclaimed.length + ' position(s) at the broker ' +
      'carry our magic but are not in our book.</b> Nothing will be ' +
      'closed automatically. Adopt them into a pair, or close them by ' +
      'hand.<table class="unclaimed"><thead><tr><th>Account</th>' +
      '<th>Ticket</th><th>Symbol</th><th>Side</th><th>Volume</th>' +
      '<th>Open</th><th></th></tr></thead><tbody>';
    unclaimed.forEach(function (row) {
      html += '<tr><td>' + row.account + '</td><td>' + row.ticket +
        '</td><td>' + row.symbol + '</td><td>' + row.side + '</td><td>' +
        fmt(row.volume, 2) + '</td><td>' + fmt(row.price_open, 4) +
        '</td><td><button class="btn close-unclaimed" data-account="' +
        row.account + '" data-ticket="' + row.ticket +
        '">Close it</button></td></tr>';
    });
    html += '</tbody></table>';
    banner.innerHTML = html;
    banner.classList.remove('hidden');
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
        : parts[0] === 'grid' ? 'Market Grid'
          : parts[0] === 'settings' ? 'Exchanges' : 'Positions';
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

  //: The keypad presets, in the order the reference screen has them.
  var QUANTITY_KEYS = {'1': 1, '2': 5, '3': 10, '4': 50, '5': 100};

  function onKey(e) {
    if (e.key === 'Escape') {
      closeModal();
      el('help-overlay').classList.add('hidden');
      return;
    }
    // Never steal a key from a field being typed into.
    var tag = (document.activeElement || {}).tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') { return; }
    if (e.metaKey || e.ctrlKey || e.altKey) { return; }

    if (e.key === '?') {
      el('help-overlay').classList.toggle('hidden');
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      return focusNextLadder();
    }

    var key = activeLadder();
    if (!key) { return; }
    var lower = e.key.toLowerCase();

    if (QUANTITY_KEYS[e.key] !== undefined) {
      state.armed[key] = QUANTITY_KEYS[e.key];
      return render();
    }
    if (e.key === '0') { state.armed[key] = null; return render(); }
    if (lower === 'b') { return atTouch(key, 'BUY'); }
    if (lower === 's') { return atTouch(key, 'SELL'); }
    if (lower === 'f') { return flatten(key); }
    if (lower === 'x') { return send('cancel_where', {pair: key}); }
    if (lower === 'l') {
      state.locked[key] = !state.locked[key];
      return render();
    }
    if (lower === 'm') {
      var pair = state.snapshot.pairs[key] || {};
      return setPair(key, {order_type: pair.order_type === 'MARKET'
                           ? 'LIMIT' : 'MARKET'});
    }
  }

  function activeLadder() {
    if (!state.active || state.active.indexOf('ladder:') !== 0) { return null; }
    return state.active.slice('ladder:'.length);
  }

  function focusNextLadder() {
    var ladders = state.open.filter(function (id) {
      return id.indexOf('ladder:') === 0;
    });
    if (!ladders.length) { return; }
    var at = ladders.indexOf(state.active);
    state.active = ladders[(at + 1) % ladders.length];
    render();
  }

  // -- polling -------------------------------------------------------------

  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(function (snapshot) {
        state.snapshot = snapshot;
        // A ghost whose order is now in the book has done its job.
        state.pending.forEach(function (ghost) {
          var pair = snapshot.pairs[ghost.pair_key] || {};
          var arrived = (pair.orders || []).some(function (order) {
            return order.side === ghost.side &&
              Math.abs(order.level - ghost.level) < 1e-9;
          }) || (pair.positions || []).some(function (position) {
            return position.opened_at * 1000 > ghost.at - 2000;
          });
          if (arrived) { ghost.done = true; }
        });
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
    el('open-settings').addEventListener('click', function () {
      openPanel(panelId('settings'));
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
    el('unclaimed-banner').addEventListener('click', function (e) {
      var button = e.target.closest('.close-unclaimed');
      if (!button) { return; }
      ask('Close ' + button.dataset.account + ':' + button.dataset.ticket +
          '?',
          'This position is at the broker but not in our book. Closing it ' +
          'is by ticket, at market, and cannot be undone.',
          'Close it', function () {
            send('close_unclaimed', {account: button.dataset.account,
                                     ticket: button.dataset.ticket});
          });
    });
    el('help').addEventListener('click', function () {
      el('help-overlay').classList.toggle('hidden');
    });
    el('help-close').addEventListener('click', function () {
      el('help-overlay').classList.add('hidden');
    });
    el('modal-cancel').addEventListener('click', closeModal);
    el('modal-confirm').addEventListener('click', function () {
      var handler = el('modal')._onConfirm;
      closeModal();
      if (handler) { handler(); }
    });
    document.addEventListener('keydown', onKey);
    window.addEventListener('click', function (e) {
      var window_ = e.target.closest('.window');
      if (!window_) { return; }
      state.active = window_.classList.contains('market-grid') ? panelId('grid')
        : window_.classList.contains('monitor') ? panelId('monitor')
          : window_.classList.contains('settings') ? panelId('settings')
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

  // The settings panel lives in its own file and borrows these: one
  // modal, one toast rack, one command path. A second copy of any of
  // them is a second set of house rules to keep.
  window.MT5Trader = {
    state: state, render: render, toast: toast, ask: ask, send: send,
    panelId: panelId, openPanel: openPanel, closePanel: closePanel,
    fmt: fmt, money: money, DASH: DASH
  };
})();
