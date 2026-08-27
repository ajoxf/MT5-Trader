"""Configuration: accounts, pairs and settings in one JSON file.

Credentials never live in that file. Each account names an environment
variable (`password_env`) holding its password, and the value lives in
`.env`, gitignored, written by the UI. Never in code, never in config,
never in chat, never in a log line.

Two things in here are load-bearing beyond "read some settings":

- **`save_raw` writes through a tmp file and `os.replace`.** A plain
  `open(path, 'w')` truncates, and a reader in that window sees half a
  config — which, in front of a read-modify-write save, wrote an EMPTY
  config back and deleted every account.
- **The clash refusals** (`endpoint_clash`, `login_clash`,
  `terminal_clash`) are checked at SAVE time, not only at startup. A
  refusal at save is a corrected field; a refusal at startup is five
  restart attempts with the reason scrolling past.
"""

import json
import logging
import os
import re

from .models import OrderType, OvernightMode, TimeInForce

try:
    from dotenv import load_dotenv
except ImportError:            # dotenv optional; the shell can set them
    load_dotenv = None


#: Defaults for every tunable the engine reads. Each one is a visible
#: setting in the UI — the spec's rule is that guessed numbers get
#: corrected from measurement, which needs them on screen first.
DEFAULT_SETTINGS = {
    # --- the loop -----------------------------------------------------
    'POLL_INTERVAL_SEC': 0.3,
    #: How often the coordinator drains clicks, on its own thread. This
    #: is the click-to-order latency the trader actually feels, and it
    #: is deliberately far shorter than the poll: waiting for the next
    #: poll would put up to a whole interval between the click and the
    #: order, on a product whose promise is that one click is one order.
    'COMMAND_POLL_SEC': 0.02,
    'ACCOUNT_INFO_CACHE_SEC': 5.0,     # an IPC round trip; do not poll it

    # --- execution ----------------------------------------------------
    #: The naked window's ceiling. The crossing order goes IMMEDIATELY
    #: on fill; this is a failure-ESCALATION window, not patience. A
    #: market order round-trips in ~24ms, so 2.0s is ~80x headroom and
    #: only fires on a real fault (spec §4, decision 2).
    'LEG_DEADLINE_SEC': 2.0,
    #: Keep a partially-matched pair only if it is at least this
    #: fraction of the clip; otherwise unwind all of it.
    'MIN_MATCHED_FRACTION': 0.4,
    #: How far through the clicked spread a MARKET click may fill before
    #: it is refused, in ladder increments. Default a few; measure the
    #: real distribution before fixing it (spec, open question 12).
    'MARKET_PROTECTION_TICKS': 3.0,
    #: ONE CLICK IS ONE ORDER. A market click crosses immediately —
    #: that is the product, and the arming is made unmistakable instead
    #: (the mode badge, the tinted click columns, the cursor). Turn this
    #: on and every market click asks first; it is a deliberate choice
    #: for a desk that wants the extra gesture, not the default.
    'CONFIRM_MARKET_CLICKS': False,
    #: A click AWAY from the touch, in MARKET mode, rests as a working
    #: order instead of being refused. A buy under the offer cannot
    #: cross at any price, and "rest it here" is what a trader means by
    #: clicking there — refusing it made the whole far side of the
    #: ladder dead. Turn this off to have such a click refused instead.
    'CLICK_AWAY_RESTS': True,
    #: Refuse to trade a pair whose two accounts turn out to be one MT5
    #: login. OFF: one account carrying both legs is an ordinary spread
    #: (spot and the future at one broker), and only the desk knows
    #: whether that is what was meant. It is always SAID — a banner
    #: names the login and both readings — but it is not refused unless
    #: this is turned on.
    'REFUSE_SHARED_ACCOUNT': False,
    #: How often the ladder re-centres itself on the mid, in seconds.
    #: 0 re-centres only when the market leaves the visible window; the
    #: Lock tick on the rail stops it entirely. A ladder that re-centres
    #: under a click is how a trader clicks the wrong price, so this is
    #: a comfort setting with a real edge to it.
    'RECENTRE_SEC': 5.0,
    #: Ladder row height in pixels. 17 is the reference screen's; a
    #: bigger target is a faster, safer click on a large monitor.
    'ROW_HEIGHT_PX': 17,
    #: Re-peg dead band, in ladder increments. Every MODIFY loses queue
    #: position, so re-pricing three times a second guarantees you are
    #: never at the front of a queue — which defeats quoting entirely
    #: (spec, open question 11).
    'REPEG_DEAD_BAND_TICKS': 1.0,
    'SLIPPAGE_POINTS': 1.0,

    # --- guards on the price itself (spec §8) -------------------------
    'MAX_QUOTE_AGE_SEC': 5.0,          # 0 = off
    'MAX_SPREAD_JUMP_SIGMA': 5.0,      # 0 = off
    'JUMP_SETTLE_SEC': 2.0,
    'SIGMA_WINDOW_QUOTES': 600,

    # --- the session clock (spec §3.1, §3.2) --------------------------
    #: One cutoff, on the BROKER's clock, shared by the DAY cancel and
    #: the overnight rule, so the trader configures a single time — and
    #: configures it in the broker's terms, which is how a trading day
    #: is actually reckoned.
    #: How often the broker's clock offset is re-measured. It does not
    #: drift on the scale of a poll, and it is a round trip per account.
    'BROKER_CLOCK_TTL_SEC': 300.0,
    'OVERNIGHT_CLOSE_HOUR': 16,
    'OVERNIGHT_CLOSE_MINUTE': 55,
    'OVERNIGHT_DEFAULT': OvernightMode.ALLOW.value,

    #: Margin level (%) below which the monitor calls an account tight.
    #: Margin is posted PER ACCOUNT with two brokers, so the WEAKEST
    #: account governs what the pair can carry — not the total.
    'MARGIN_WARN_LEVEL': 200.0,

    # --- costs --------------------------------------------------------
    'SPREAD_COST_FACTOR': 1.0,
    'COMMISSION_PER_LOT_A': 0.0,
    'COMMISSION_PER_LOT_B': 0.0,

    # --- housekeeping -------------------------------------------------
    'RECONCILE_INTERVAL_SEC': 20.0,
    'CLOSE_ATTEMPTS': 3,
    #: 'ask' / 'always' / 'never' — what shutdown does with open
    #: positions. An unanswered prompt means NO (spec §12).
    'SHUTDOWN_CLOSE_POSITIONS': 'ask',
}

#: Settings the launcher reads at STARTUP. Changing one needs a restart
#: and must say so; everything else hot-applies. Crying "restart" on
#: every save teaches the operator to ignore the line that matters.
STRUCTURAL_SETTINGS = ('POLL_INTERVAL_SEC',)


class AccountConfig:
    """One MT5 account = one login, one terminal, one port.

    Two accounts pointing at the same terminal folder are ONE account
    whatever the config says — a terminal holds a single login.
    """

    def __init__(self, name, terminal_path=None, login=None,
                 password_env="", server=None, endpoint=None):
        self.name = name
        self.terminal_path = terminal_path
        self.login = int(login) if login else None
        self.password_env = password_env or env_key_for(name)
        self.server = server
        #: host:port where this account's leg runner listens.
        self.endpoint = endpoint

    @property
    def password(self):
        return os.environ.get(self.password_env) if self.password_env else None

    def to_dict(self):
        return {'terminal_path': self.terminal_path, 'login': self.login,
                'password_env': self.password_env, 'server': self.server,
                'endpoint': self.endpoint}

    @classmethod
    def from_dict(cls, name, raw):
        raw = raw or {}
        return cls(name, terminal_path=raw.get('terminal_path'),
                   login=raw.get('login'),
                   password_env=raw.get('password_env'),
                   server=raw.get('server'), endpoint=raw.get('endpoint'))


class PairConfig:
    """One ladder: two symbols on two accounts, and how they are traded.

    Contract sizes and volume steps are NOT typed in — they are read
    from MT5 and cached here so the UI can render before the runners
    answer. `hedge_ratio_for` stamps beta with the pair it was computed
    for, so a stale beta from the previous instrument cannot silently
    define the spread (spec §2).
    """

    def __init__(self, key, name=None, leg_a=None, leg_b=None,
                 hedge_ratio=1.0, hedge_ratio_for=None, pair_type='SPOT_FUTURE',
                 increment=None, clip_lots_a=None, clip_lots_b=None,
                 default_quantity=1.0, order_type=OrderType.LIMIT.value,
                 time_in_force=TimeInForce.DAY.value,
                 overnight=OvernightMode.ALLOW.value,
                 quoting_leg=None, enabled=True, rows=30,
                 expiry=None, swap_per_day=None):
        self.key = key
        self.name = name or key
        self.leg_a = dict(leg_a or {})      # {'account': ..., 'symbol': ...}
        self.leg_b = dict(leg_b or {})
        self.hedge_ratio = float(hedge_ratio or 1.0)
        self.hedge_ratio_for = hedge_ratio_for
        self.pair_type = pair_type
        #: Spread ticks per ladder row. None = derive it (see
        #: `derived_increment`) rather than guess a readable-looking one.
        self.increment = increment
        #: What ONE spread means in leg lots. None until the matched
        #: minimum can be computed from both legs' MT5 metadata.
        self.clip_lots_a = clip_lots_a
        self.clip_lots_b = clip_lots_b
        self.default_quantity = float(default_quantity or 1.0)
        self.order_type = OrderType(order_type)
        self.time_in_force = TimeInForce(time_in_force)
        self.overnight = OvernightMode(overnight)
        #: Which leg rests the real pending in LIMIT mode. None = pick
        #: the wider bid-ask (that is the spread being earned), measured
        #: not assumed (spec §4, open question 10).
        self.quoting_leg = quoting_leg
        self.enabled = bool(enabled)
        self.rows = int(rows)
        #: The futures leg's expiry, and what carrying one spread for
        #: one day is worth at THIS broker on THIS account, in spread
        #: points. Neither is derivable from the price feed, and both
        #: are what turn a basis into a fair value rather than a number
        #: that happens to oscillate. Unset = no fair value shown.
        self.expiry = expiry
        self.swap_per_day = (None if swap_per_day in (None, '')
                             else float(swap_per_day))
        #: Cached MT5 metadata per leg, refreshed by the coordinator.
        self.meta_a = {}
        self.meta_b = {}

    @property
    def symbol_a(self):
        return self.leg_a.get('symbol')

    @property
    def symbol_b(self):
        return self.leg_b.get('symbol')

    @property
    def account_a(self):
        return self.leg_a.get('account')

    @property
    def account_b(self):
        return self.leg_b.get('account')

    def derived_increment(self):
        """`max(tick_B, beta x tick_A)` — the smallest step the spread
        can actually move in (spec, decision 6).

        Returns None when the legs' tick sizes are not known yet, which
        the UI renders as "—" rather than as a number it made up.
        """
        tick_a = (self.meta_a or {}).get('tick_size')
        tick_b = (self.meta_b or {}).get('tick_size')
        if not tick_a or not tick_b:
            return None
        return max(float(tick_b), float(self.hedge_ratio or 1.0) * float(tick_a))

    def effective_increment(self):
        """What the ladder actually steps by: the override, or derived."""
        return self.increment or self.derived_increment()

    def to_dict(self):
        return {
            'name': self.name, 'leg_a': dict(self.leg_a),
            'leg_b': dict(self.leg_b), 'hedge_ratio': self.hedge_ratio,
            'hedge_ratio_for': self.hedge_ratio_for,
            'pair_type': self.pair_type, 'increment': self.increment,
            'clip_lots_a': self.clip_lots_a, 'clip_lots_b': self.clip_lots_b,
            'default_quantity': self.default_quantity,
            'order_type': self.order_type.value,
            'time_in_force': self.time_in_force.value,
            'overnight': self.overnight.value,
            'quoting_leg': self.quoting_leg,
            'enabled': self.enabled, 'rows': self.rows,
            'expiry': self.expiry, 'swap_per_day': self.swap_per_day,
        }

    @classmethod
    def from_dict(cls, key, raw):
        raw = dict(raw or {})
        raw.pop('key', None)
        return cls(key, **raw)


class TraderConfig:
    """Everything the engine reads, and where it came from."""

    def __init__(self, accounts=None, pairs=None, settings=None, path=None):
        self.accounts = dict(accounts or {})
        self.pairs = dict(pairs or {})
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(settings or {})
        self.path = path

    def get(self, key, default=None):
        return self.settings.get(key, DEFAULT_SETTINGS.get(key, default))

    def enabled_pairs(self):
        return {k: p for k, p in self.pairs.items() if p.enabled}

    @classmethod
    def from_raw(cls, raw, path=None):
        raw = raw or {}
        accounts = {name: AccountConfig.from_dict(name, acct)
                    for name, acct in (raw.get('accounts') or {}).items()}
        pairs = {key: PairConfig.from_dict(key, pair)
                 for key, pair in (raw.get('pairs') or {}).items()}
        return cls(accounts, pairs, raw.get('settings'), path)

    @classmethod
    def from_file(cls, path):
        if load_dotenv is not None:
            load_dotenv(os.path.join(os.path.dirname(os.path.abspath(path))
                                     or '.', '.env'))
        return cls.from_raw(load_raw(path), path)

    def to_raw(self):
        return {
            'accounts': {n: a.to_dict() for n, a in self.accounts.items()},
            'pairs': {k: p.to_dict() for k, p in self.pairs.items()},
            'settings': dict(self.settings),
        }

    def restart_required(self, fresh):
        """Which changes the launcher only reads at STARTUP.

        Compare only what actually needs a restart — accounts, symbols,
        beta, contract sizes and the poll interval. Display and comfort
        settings hot-apply.
        """
        changes = []
        if {n: a.to_dict() for n, a in self.accounts.items()} != \
                {n: a.to_dict() for n, a in fresh.accounts.items()}:
            changes.append('accounts')
        for key in set(self.pairs) | set(fresh.pairs):
            old, new = self.pairs.get(key), fresh.pairs.get(key)
            if (old is None) != (new is None):
                changes.append(f'pair {key}')
                continue
            structural = ('leg_a', 'leg_b', 'hedge_ratio', 'enabled')
            if any(getattr(old, f) != getattr(new, f) for f in structural):
                changes.append(f'pair {key}')
        for key in STRUCTURAL_SETTINGS:
            if self.get(key) != fresh.get(key):
                changes.append(key)
        return changes


# --- reading and writing, safely ---------------------------------------

def load_raw(path):
    """The config as a plain dict.

    MISSING is legitimately empty (first run). PRESENT-BUT-BROKEN falls
    back to the `.bak` beside it, and failing that RAISES — a tolerant
    reader is precisely wrong here, because returning {} in front of a
    read-modify-write save is how every account gets deleted.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        try:
            with open(path + '.bak', 'r', encoding='utf-8') as f:
                backup = json.load(f)
        except (OSError, ValueError):
            raise RuntimeError(
                f'{os.path.basename(path)} could not be read ({e}) and there '
                f'is no usable backup beside it. Refusing to continue, '
                f'because saving now would overwrite it with nothing.'
            ) from None
        logging.error('%s unreadable (%s) — using the .bak. The next save '
                      'will rewrite the good copy.', path, e)
        return backup


#: Top-level keys whose disappearance is a catastrophe rather than an
#: edit: they are what lets the engine start at all.
CRITICAL_KEYS = ('accounts', 'pairs')


def save_raw(path, raw, allow_shrink=False):
    """Write the config, keeping a backup and refusing to gut it.

    `allow_shrink` is for the endpoints that legitimately remove things
    (deleting an account or a pair). Everything else is a partial edit
    and must not be able to drop a section it never meant to touch.
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            current = json.load(f)
    except (OSError, ValueError):
        current = None
    if current and not allow_shrink:
        lost = [key for key in CRITICAL_KEYS
                if current.get(key) and not raw.get(key)]
        if lost:
            raise RuntimeError(
                'refusing to save a config that would drop '
                + ', '.join(lost)
                + ' — this looks like a partial read, not an edit')
    if current is not None:
        tmp_bak = path + '.bak.tmp'
        with open(tmp_bak, 'w', encoding='utf-8') as f:
            json.dump(current, f, indent=2)
        os.replace(tmp_bak, path + '.bak')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(raw, f, indent=2)
    os.replace(tmp, path)


# --- the three clashes, refused at SAVE time ---------------------------

def endpoint_clash(raw, name, endpoint):
    """Is another account already on this endpoint? Message or None.

    Only one process can listen on a port. Two accounts sharing one
    means the second leg runner cannot start — or, if the first won the
    race, BOTH legs connect to it and trade the SAME MT5 account while
    every screen reports two.
    """
    if not endpoint:
        return None                # blank = no runner; any number may
    for other, acct in (raw.get('accounts') or {}).items():
        if other == name:
            continue
        if ((acct or {}).get('endpoint') or '').strip() == endpoint:
            return (f"Endpoint {endpoint} already belongs to account "
                    f"'{other}'. One port serves ONE leg runner — give this "
                    f"account its own (e.g. {next_free_port(raw)}), or both "
                    f"legs would end up on the same terminal.")
    return None


def login_clash(raw, name, login):
    """Two rows with one login is the same MT5 account twice: both legs
    would trade it and hedge against themselves."""
    if not login:
        return None
    for other, acct in (raw.get('accounts') or {}).items():
        if other == name:
            continue
        if str((acct or {}).get('login') or '') == str(login):
            return (f"Login {login} already belongs to account '{other}'. "
                    f"Two accounts on one login is the same MT5 account "
                    f"twice — both legs would trade it and hedge against "
                    f"themselves.")
    return None


def terminal_clash(raw, name, path):
    """One account needs one PORT, one LOGIN and one TERMINAL.

    A terminal holds a single login, so two accounts sharing an
    installation are one account: both leg runners attach to it, both
    trade whatever it happens to be signed into, and the pair hedges
    against itself.
    """
    path = (path or '').strip()
    if not path:
        return None                # blank = attach to whatever is open
    for other, acct in (raw.get('accounts') or {}).items():
        if other == name:
            continue
        if ((acct or {}).get('terminal_path') or '').strip().lower() \
                == path.lower():
            return (f"Account '{other}' already uses this MT5 installation. "
                    f"One terminal serves ONE login, so both legs would end "
                    f"up on the same account. Install a second copy of "
                    f"MetaTrader 5 in its own folder (or use a portable "
                    f"copy) and point this account at that one.")
    return None


def next_free_port(raw, host='127.0.0.1', first=9101):
    used = {(acct or {}).get('endpoint', '') for acct
            in (raw.get('accounts') or {}).values()}
    port = first
    while f'{host}:{port}' in used:
        port += 1
    return f'{host}:{port}'


# --- secrets ------------------------------------------------------------

_ENV_SAFE = re.compile(r'[^A-Z0-9_]')


def env_key_for(account_name):
    """The .env key for an account's password.

    Sanitised, because an account named `Ut 2` produced
    `MT5_PASSWORD_UT 2` — a key with a space, which dotenv cannot parse,
    so the password silently never loaded.
    """
    slug = _ENV_SAFE.sub('_', (account_name or '').upper().strip())
    slug = re.sub(r'_+', '_', slug).strip('_') or 'ACCOUNT'
    return f'MT5_PASSWORD_{slug}'


def env_line(key, value):
    """One `.env` line, quoted so passwords with spaces or `#` survive."""
    escaped = str(value or '').replace('\\', '\\\\').replace('"', '\\"')
    return f'{key}="{escaped}"'


def write_env_value(path, key, value):
    """Set one key in `.env`, leaving the rest of the file alone.

    Written through a tmp file and `os.replace` for the same reason the
    config is: a truncated `.env` is every account's password gone.
    """
    lines = []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        pass
    replaced = False
    out = []
    for line in lines:
        if line.split('=', 1)[0].strip() == key:
            if not replaced:
                out.append(env_line(key, value))
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(env_line(key, value))
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, path)
    os.environ[key] = str(value or '')
    try:
        os.chmod(path, 0o600)
    except OSError:               # not every filesystem allows it
        pass
