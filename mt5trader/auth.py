"""Who is allowed to work this terminal.

Until now the only thing standing between a browser and the order
button was that the server listened on 127.0.0.1. That is enough while
the box is the trader's own desk and nobody else ever touches it. It is
not enough the moment this ships as an installed product: a shared
Windows box, a remote desktop left signed in, a colleague who opens the
laptop — every one of those reaches the loopback address, and the
ladder places live orders with one click.

So the terminal is now bound to localhost **and** behind a login.

What is stored here, and what is not:

- **The password is never stored.** What is on disk is a scrypt hash
  produced by `werkzeug.security`, which is a one-way function with a
  per-password salt. A stolen `auth.json` does not yield the password.
- **Broker credentials are not here.** They live in `.env` and nowhere
  else, exactly as before. This file knows about the person at the
  keyboard, not about the accounts.
- **The signing key is generated once, on this machine**, and kept
  beside the hashes. It has to persist: a key regenerated at startup
  would sign every trader out on every restart of a process that is
  restarted routinely.

There is no default password. A terminal that ships with one is a
terminal that runs with one, and the first-run screen asking the trader
to choose costs them ten seconds once.
"""

import json
import logging
import os
import secrets
import time

from werkzeug.security import check_password_hash, generate_password_hash

from . import atomicfile

#: Wrong passwords in a row before the account is held shut. Five is
#: generous for someone typing their own password and hopeless for
#: someone guessing.
MAX_FAILURES = 5

#: How long the account stays shut once it is. Long enough that guessing
#: is pointless, short enough that the trader's own fat finger does not
#: cost them the session — the market does not wait for a lockout.
LOCKOUT_SEC = 300.0

#: How long a session may sit idle before it has to log in again. A
#: trading day is longer than this; a lunch break is not.
IDLE_SEC = 8 * 3600.0

#: The shortest password the terminal will accept. Short enough not to
#: be theatre, long enough that MAX_FAILURES is not the only defence.
MIN_PASSWORD = 10


class AuthError(Exception):
    """A refusal, in words fit to put on the screen.

    Never "invalid credentials, check the log": the person reading this
    is the owner of the machine, and the reason they cannot get in is
    the whole content of the message.
    """


def _now():
    return time.time()


class Store:
    """The account file: read on demand, written atomically.

    Read on demand rather than cached because the login page and the
    engine are different processes on the same box, and a password
    changed in one must be true in the other on the next attempt.
    """

    def __init__(self, path):
        self.path = str(path)

    # -- the file ----------------------------------------------------------

    def load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                body = json.load(f)
        except FileNotFoundError:
            return {'version': 1, 'users': {}}
        except (OSError, ValueError) as e:
            # Refusing to start is the right answer: carrying on with an
            # empty store would silently offer first-run setup to
            # whoever corrupted the file.
            raise AuthError(f'the account file {self.path} could not be '
                            f'read ({e}). Nobody can be signed in until it '
                            f'is repaired or removed.')
        if not isinstance(body, dict) or not isinstance(
                body.get('users'), dict):
            raise AuthError(f'the account file {self.path} is not an account '
                            f'file. Nobody can be signed in until it is '
                            f'repaired or removed.')
        return body

    def save(self, body):
        tmp = self.path + '.tmp'
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(body, f, indent=2, sort_keys=True)
        _own_it(tmp)
        if not atomicfile.replace(tmp, self.path):
            raise AuthError(f'the account file {self.path} could not be '
                            f'written — it may be open in another program.')
        _own_it(self.path)

    # -- the signing key ---------------------------------------------------

    def secret_key(self):
        """The cookie signing key for THIS installation.

        Generated on first use and kept. Sessions are signed with it, so
        a new key signs everyone out — which is correct after a wipe and
        wrong after a restart.
        """
        body = self.load()
        key = body.get('secret_key')
        if not key:
            key = secrets.token_hex(32)
            body['secret_key'] = key
            self.save(body)
        return key

    # -- accounts ----------------------------------------------------------

    def usernames(self):
        return sorted(self.load()['users'])

    def needs_setup(self):
        """True when nobody has an account yet.

        This is the ONLY state in which the login page will create one,
        and it is checked on every request rather than remembered: a
        store that gains a user must stop offering setup immediately.
        """
        return not self.load()['users']

    def create_user(self, username, password, must_change=False):
        username = _tidy_username(username)
        body = self.load()
        if username in body['users']:
            raise AuthError(f'there is already an account called {username}.')
        _check_password(password, username)
        body['users'][username] = {
            'password_hash': generate_password_hash(password),
            'must_change': bool(must_change),
            'created_at': _now(),
            'password_changed_at': _now(),
            'failures': 0,
            'locked_until': None,
        }
        self.save(body)
        logging.info('account %s created', username)
        return username

    def set_password(self, username, password):
        username = _tidy_username(username)
        body = self.load()
        user = body['users'].get(username)
        if not user:
            raise AuthError(f'there is no account called {username}.')
        _check_password(password, username)
        user['password_hash'] = generate_password_hash(password)
        user['password_changed_at'] = _now()
        user['must_change'] = False
        # A password change clears a lockout: the person who could
        # change it is the person who was locked out of it.
        user['failures'] = 0
        user['locked_until'] = None
        self.save(body)

    def must_change(self, username):
        user = self.load()['users'].get(_tidy_username(username)) or {}
        return bool(user.get('must_change'))

    def lock_note(self, username, now=None):
        """How long this account stays shut, in words, or None."""
        user = self.load()['users'].get(_tidy_username(username)) or {}
        return _lock_note(user, _now() if now is None else now)

    # -- the attempt itself ------------------------------------------------

    def authenticate(self, username, password, now=None):
        """Check a password. Raises AuthError with the reason it failed.

        The reason distinguishes a locked account from a wrong password,
        because those need different things from the person reading it —
        one is "wait", the other is "try again". It does NOT distinguish
        an unknown username from a wrong password: that would tell
        someone guessing which half they got right.
        """
        now = _now() if now is None else now
        username = _tidy_username(username)
        body = self.load()
        user = body['users'].get(username)
        if not user:
            # Spend roughly the same time as a real check, so the reply
            # does not time out the answer to "does this account exist".
            check_password_hash(generate_password_hash('decoy-decoy'), password
                                or '')
            raise AuthError('that username and password do not match an '
                            'account on this machine.')
        note = _lock_note(user, now)
        if note:
            raise AuthError(note)
        if password and check_password_hash(user['password_hash'], password):
            user['failures'] = 0
            user['locked_until'] = None
            user['last_login_at'] = now
            self.save(body)
            return username
        user['failures'] = int(user.get('failures') or 0) + 1
        left = MAX_FAILURES - user['failures']
        if left <= 0:
            user['locked_until'] = now + LOCKOUT_SEC
            user['failures'] = 0
            self.save(body)
            logging.warning('account %s locked after %d wrong passwords',
                            username, MAX_FAILURES)
            raise AuthError(_lock_note(user, now))
        self.save(body)
        raise AuthError(f'that username and password do not match an account '
                        f'on this machine. {left} '
                        f'{"attempt" if left == 1 else "attempts"} left '
                        f'before this account is held shut for '
                        f'{_minutes(LOCKOUT_SEC)}.')


# -- session bookkeeping ---------------------------------------------------

def session_is_live(session, now=None, idle_sec=IDLE_SEC):
    """Is this cookie still a signed-in session?

    Idle is measured from the last request, not from the login, so a
    trader who is working is never signed out mid-day and a screen left
    open overnight is.
    """
    now = _now() if now is None else now
    if not session.get('user'):
        return False
    seen = session.get('seen')
    if not isinstance(seen, (int, float)):
        return False
    return (now - seen) <= idle_sec


def touch(session, now=None):
    session['seen'] = _now() if now is None else now


def start_session(session, username, now=None):
    session.clear()
    session['user'] = username
    session['csrf'] = secrets.token_urlsafe(32)
    touch(session, now)


def csrf_token(session):
    """The token this session's writes must carry.

    Minted on demand so that the first-run and login pages — which POST
    before there is a user — are covered too.
    """
    token = session.get('csrf')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf'] = token
    return token


def csrf_ok(session, presented):
    expected = session.get('csrf')
    if not expected or not presented:
        return False
    return secrets.compare_digest(str(expected), str(presented))


# -- helpers ---------------------------------------------------------------

def _tidy_username(username):
    name = str(username or '').strip().lower()
    if not name:
        raise AuthError('a username is needed.')
    return name


def _check_password(password, username):
    password = str(password or '')
    if len(password) < MIN_PASSWORD:
        raise AuthError(f'that password is {len(password)} characters. This '
                        f'terminal places live orders — {MIN_PASSWORD} or '
                        f'more.')
    if password.strip().lower() == str(username or '').strip().lower():
        raise AuthError('the password cannot be the username.')


def _lock_note(user, now):
    until = user.get('locked_until')
    if not until or until <= now:
        return None
    return (f'this account is held shut for another '
            f'{_minutes(until - now)} after {MAX_FAILURES} wrong passwords.')


def _minutes(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f'{seconds}s'
    return f'{seconds // 60}m {seconds % 60:02d}s'


def _own_it(path):
    """Make the account file readable by its owner only.

    A no-op on Windows, where the ACL inherited from the install
    directory is what governs — but on the Linux boxes the tests and the
    developer's machine run on, a world-readable hash file is a hash
    file that gets copied.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:                                   # pragma: no cover
        pass
