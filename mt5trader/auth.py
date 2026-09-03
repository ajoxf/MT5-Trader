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

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
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

#: The authenticator's step, in seconds. Thirty is what every
#: authenticator app assumes and none of them let you change.
TOTP_STEP = 30

#: How many steps either side of now are accepted. One — thirty seconds
#: of slack for a phone clock that drifts and a trader who types slowly.
#: More than that is thirty more seconds in which a shoulder-surfed code
#: still works.
TOTP_WINDOW = 1

#: Recovery codes handed over at enrolment. Ten is enough to survive a
#: lost phone more than once and few enough to print on one line each.
RECOVERY_CODES = 10

#: The name the authenticator app shows beside the code.
ISSUER = 'Nexus'


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

    # -- the second factor -------------------------------------------------

    def totp_state(self, username):
        """What this account's authenticator is: 'none', 'pending' or
        'on'."""
        user = self.load()['users'].get(_tidy_username(username)) or {}
        if not user.get('totp_secret'):
            return 'none'
        return 'on' if user.get('totp_confirmed') else 'pending'

    def load_secret(self, username):
        """The secret this account is enrolling with, or None."""
        user = self.load()['users'].get(_tidy_username(username)) or {}
        return user.get('totp_secret')

    def start_enrolment(self, username):
        """Mint a secret to show, and keep it UNCONFIRMED.

        Unconfirmed because an account whose authenticator was never
        proved to work is an account nobody can get into — the enrolment
        is not finished until a code off the phone comes back.
        """
        username = _tidy_username(username)
        body = self.load()
        user = body['users'].get(username)
        if not user:
            raise AuthError(f'there is no account called {username}.')
        secret = new_totp_secret()
        user['totp_secret'] = secret
        user['totp_confirmed'] = False
        self.save(body)
        return secret

    def confirm_enrolment(self, username, code, now=None):
        """Prove the phone works, then hand over the recovery codes.

        Returns the codes in the clear, once. What is kept is hashes:
        the file must not be a list of ways in.
        """
        now = _now() if now is None else now
        username = _tidy_username(username)
        body = self.load()
        user = body['users'].get(username) or {}
        secret = user.get('totp_secret')
        if not secret:
            raise AuthError('this account has no authenticator to confirm — '
                            'start again.')
        counter = totp_counter(secret, code, at=now)
        if counter is None:
            raise AuthError('that code is not the one this authenticator is '
                            'showing. Check the clock on the phone, and try '
                            'the next code.')
        codes = new_recovery_codes()
        salt = secrets.token_hex(16)
        user['totp_confirmed'] = True
        user['totp_last_counter'] = counter
        user['recovery_salt'] = salt
        user['recovery_hashes'] = [recovery_hash(salt, c) for c in codes]
        self.save(body)
        logging.info('account %s enrolled an authenticator', username)
        return codes

    def recovery_left(self, username):
        user = self.load()['users'].get(_tidy_username(username)) or {}
        return len(user.get('recovery_hashes') or [])

    def check_second_factor(self, username, code, now=None):
        """The six digits, or one recovery code. Raises with the reason.

        Returns 'code' or 'recovery' so the screen can say which was
        used — a trader who has just spent one of ten needs telling.
        """
        now = _now() if now is None else now
        username = _tidy_username(username)
        body = self.load()
        user = body['users'].get(username)
        if not user or not user.get('totp_confirmed'):
            raise AuthError('this account has no authenticator set up.')
        note = _lock_note(user, now)
        if note:
            raise AuthError(note)
        secret = user['totp_secret']
        counter = totp_counter(secret, code, at=now)
        if counter is not None:
            last = user.get('totp_last_counter')
            if last is not None and counter <= last:
                # Not a failure to count: it is the right code, used
                # twice. Saying so is the difference between "wait eight
                # seconds" and "your phone is wrong".
                raise AuthError('that code has already been used — wait for '
                                'the next one.')
            user['totp_last_counter'] = counter
            user['failures'] = 0
            self.save(body)
            return 'code'
        typed = _tidy_recovery(code)
        wanted = recovery_hash(user.get('recovery_salt'), typed)
        for index, hashed in enumerate(user.get('recovery_hashes') or []):
            if typed and secrets.compare_digest(hashed, wanted):
                # Single use: it is spent whether or not the phone ever
                # comes back.
                user['recovery_hashes'].pop(index)
                user['failures'] = 0
                self.save(body)
                logging.warning('account %s signed in with a recovery code — '
                                '%d left', username,
                                len(user['recovery_hashes']))
                return 'recovery'
        return self._count_failure(body, user, username, now,
                                   'that is not the code this account is '
                                   'showing, and not one of its recovery '
                                   'codes.')

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
        return self._count_failure(body, user, username, now,
                                   'that username and password do not match '
                                   'an account on this machine.')

    def _count_failure(self, body, user, username, now, sentence):
        """One wrong attempt — of either factor — against the same count.

        The same count on purpose: guessing the password and guessing
        the code are the same attack, and letting the second one start
        fresh would double the guesses on offer.
        """
        user['failures'] = int(user.get('failures') or 0) + 1
        left = MAX_FAILURES - user['failures']
        if left <= 0:
            user['locked_until'] = now + LOCKOUT_SEC
            user['failures'] = 0
            self.save(body)
            logging.warning('account %s locked after %d wrong attempts',
                            username, MAX_FAILURES)
            raise AuthError(_lock_note(user, now))
        self.save(body)
        raise AuthError(f'{sentence} {left} '
                        f'{"attempt" if left == 1 else "attempts"} left '
                        f'before this account is held shut for '
                        f'{_minutes(LOCKOUT_SEC)}.')


# -- the second factor -----------------------------------------------------
#
# RFC 6238, implemented here rather than pulled in, because it is thirty
# lines of stdlib and this is a product that installs on a machine with
# no internet: every dependency is something the installer has to carry
# and something that can fail to be there at 7am.

def new_totp_secret():
    """A fresh authenticator secret: 160 bits, base32, no padding."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')


def totp_code(secret, at=None, step=TOTP_STEP):
    """The six digits an authenticator would be showing right now."""
    at = _now() if at is None else at
    return _hotp(secret, int(at // step))


def totp_counter(secret, code, at=None, step=TOTP_STEP, window=TOTP_WINDOW):
    """The step this code belongs to, or None if it belongs to none.

    The COUNTER, not True, because a code that was right thirty seconds
    ago is right again for as long as its step lasts — and a code that
    has been used once must not work a second time.
    """
    code = _tidy_code(code)
    if len(code) != 6 or not code.isdigit():
        return None
    at = _now() if at is None else at
    here = int(at // step)
    for offset in range(-window, window + 1):
        if secrets.compare_digest(_hotp(secret, here + offset), code):
            return here + offset
    return None


def otpauth_uri(secret, username, issuer=ISSUER):
    """What the QR code on the enrolment page encodes."""
    from urllib.parse import quote
    label = quote(f'{issuer}:{username}')
    return (f'otpauth://totp/{label}?secret={secret}'
            f'&issuer={quote(issuer)}&algorithm=SHA1&digits=6'
            f'&period={TOTP_STEP}')


def new_recovery_codes(count=RECOVERY_CODES):
    """Codes for the day the phone is lost.

    Shown once, stored as hashes, and each one works once. They are the
    password reset too: there is no server to email a link from, and a
    trader locked out of their own installed terminal has nobody to ring.

    Sixty-four bits each, because these are not passwords: a password is
    hashed slowly because a human chose it and a guesser can therefore
    enumerate the likely ones. Nobody chose these — they come out of the
    system's own randomness, and there is no shortlist to try.
    """
    return [f'{secrets.token_hex(4)}-{secrets.token_hex(4)}'
            for _ in range(count)]


def recovery_hash(salt, code):
    """A recovery code as it is kept: salted SHA-256, not scrypt.

    Deliberately the fast one. Slow hashing buys nothing against a
    64-bit random secret — 2**64 SHA-256 evaluations is out of reach on
    its own — and it costs a full second at enrolment, on a screen the
    trader is standing in front of. Passwords still get scrypt, because
    a password IS guessable.
    """
    return hashlib.sha256(
        (str(salt) + _tidy_recovery(code)).encode('utf-8')).hexdigest()


def _hotp(secret, counter):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack('>Q', int(counter)),
                      hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f'{number % 1_000_000:06d}'


def _tidy_code(code):
    """What the trader typed, without the spaces the app puts in."""
    return re.sub(r'[\s]', '', str(code or ''))


def _tidy_recovery(code):
    return re.sub(r'[^0-9a-f]', '', str(code or '').strip().lower())


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
            f'{_minutes(until - now)} after {MAX_FAILURES} wrong '
            f'attempts.')


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
