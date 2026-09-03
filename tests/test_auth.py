"""The door.

Loopback kept the terminal off the network. It never kept anyone off the
KEYBOARD, and this ships as an installed product now: a shared Windows
box, a remote desktop left signed in, a laptop opened by someone else —
every one of those reaches 127.0.0.1, and one click on the ladder sends
a live order.

So each test here is about a way in that must not exist:

- an API that answers before anyone has signed in;
- an order placed by a page that was never issued a token;
- a password read back off the disk;
- a guessed password, tried a thousand times;
- a session left open overnight;
- a `next` parameter that leaves this machine.
"""

import json
import re
import time

import pytest

from conftest import TEST_PASSWORD, signed_in
from mt5trader import auth
from mt5trader.webapp import create_app

GOOD = 'ladder-pass-2026'


@pytest.fixture
def paths(tmp_path):
    return {
        'status': str(tmp_path / 'status.json'),
        'commands': str(tmp_path / 'commands.jsonl'),
        'results': str(tmp_path / 'results.json'),
        'config': str(tmp_path / 'config.json'),
        'db': str(tmp_path / 'trader.db'),
        'auth': str(tmp_path / 'auth.json'),
    }


@pytest.fixture
def app(paths):
    app = create_app(paths['status'], paths['commands'], paths['results'],
                     paths['config'], paths['db'], paths['auth'])
    app.config.update(TESTING=True)
    return app


@pytest.fixture
def store(paths):
    return auth.Store(paths['auth'])


def csrf_of(client):
    with client.session_transaction() as session:
        return auth.csrf_token(session)


# -- the store -------------------------------------------------------------

def test_the_password_is_never_written_to_disk(store, paths):
    """A stolen account file must not yield the password. What is stored
    is a scrypt hash with its own salt, which does not run backwards."""
    store.create_user('trader', GOOD)

    raw = open(paths['auth'], encoding='utf-8').read()

    assert GOOD not in raw
    body = json.loads(raw)
    hashed = body['users']['trader']['password_hash']
    assert hashed.startswith('scrypt:')
    # ...and the control: the hash it did store checks out, so this is a
    # password that was kept, not a password that was dropped.
    assert store.authenticate('trader', GOOD) == 'trader'


def test_a_wrong_password_is_refused(store):
    store.create_user('trader', GOOD)

    with pytest.raises(auth.AuthError) as refusal:
        store.authenticate('trader', GOOD + 'x')

    assert 'do not match' in str(refusal.value)


def test_an_unknown_account_does_not_say_it_is_unknown(store):
    """Telling someone which HALF they got right halves the guessing."""
    store.create_user('trader', GOOD)

    with pytest.raises(auth.AuthError) as unknown:
        store.authenticate('nobody', GOOD)
    with pytest.raises(auth.AuthError) as wrong:
        store.authenticate('trader', 'not-the-password')

    # The wrong-password message adds the attempts left; the sentence
    # about what did not match is the same either way.
    assert str(unknown.value).startswith(
        'that username and password do not match')
    assert str(wrong.value).startswith(
        'that username and password do not match')


def test_guessing_holds_the_account_shut(store):
    """Five wrong passwords and the account stops answering, so a
    dictionary run costs the guesser five minutes per five guesses."""
    store.create_user('trader', GOOD)
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            store.authenticate('trader', 'wrong')

    # Now even the RIGHT password is refused, and it says why.
    with pytest.raises(auth.AuthError) as shut:
        store.authenticate('trader', GOOD)
    assert 'held shut for another' in str(shut.value)


def test_the_lockout_expires(store):
    """It is a delay, not a wall: the trader's own fat finger must not
    cost them the session — the market does not wait for a lockout."""
    store.create_user('trader', GOOD)
    now = time.time()
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            store.authenticate('trader', 'wrong', now=now)

    assert store.authenticate('trader', GOOD,
                              now=now + auth.LOCKOUT_SEC + 1) == 'trader'


def test_a_right_password_clears_the_count(store):
    """Four wrong over a morning, then the right one, must not leave the
    account one typo from being shut."""
    store.create_user('trader', GOOD)
    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError):
            store.authenticate('trader', 'wrong')

    assert store.authenticate('trader', GOOD) == 'trader'

    for _ in range(auth.MAX_FAILURES - 1):
        with pytest.raises(auth.AuthError) as again:
            store.authenticate('trader', 'wrong')
    # Still counting from zero: the fourth wrong password since the
    # right one, with one attempt left rather than an account shut.
    assert '1 attempt left' in str(again.value)


def test_a_short_password_is_refused_with_its_own_length(store):
    with pytest.raises(auth.AuthError) as refusal:
        store.create_user('trader', 'short')

    assert '5 characters' in str(refusal.value)
    assert str(auth.MIN_PASSWORD) in str(refusal.value)


def test_the_signing_key_survives_a_restart(paths):
    """A key regenerated at startup signs every trader out on every
    restart of a process that is restarted routinely."""
    first = auth.Store(paths['auth']).secret_key()

    assert auth.Store(paths['auth']).secret_key() == first
    assert len(first) >= 32


def test_a_corrupt_account_file_does_not_read_as_a_fresh_install(paths):
    """Reading it as empty would offer first-run setup — a new account,
    on a machine that already has one — to whoever corrupted it."""
    open(paths['auth'], 'w', encoding='utf-8').write('{ not json')

    with pytest.raises(auth.AuthError) as refusal:
        auth.Store(paths['auth']).needs_setup()

    assert 'could not be read' in str(refusal.value)


# -- the endpoints ---------------------------------------------------------

def test_nothing_answers_before_anyone_signs_in(app, store):
    store.create_user('trader', GOOD)
    client = app.test_client()

    assert client.get('/api/status').status_code == 401
    assert client.get('/api/settings').status_code == 401
    assert client.get('/api/fills').status_code == 401
    assert client.post('/api/command',
                       json={'kind': 'flatten_pair'}).status_code == 401
    # The ladder itself is a redirect to the login page, not a 401: a
    # browser asking for a PAGE should be shown one.
    page = client.get('/')
    assert page.status_code == 302 and '/login' in page.headers['Location']


def test_the_control_a_signed_in_client_is_served(app, store):
    """Without this the test above would pass on a server that was
    simply broken."""
    client = signed_in(app)

    assert client.get('/api/status').status_code == 200
    assert client.get('/api/settings').status_code == 200


def test_an_expired_session_is_not_a_signed_in_one(app):
    """A screen left open overnight is a screen anyone can click."""
    client = signed_in(app)
    with client.session_transaction() as session:
        session['seen'] = time.time() - auth.IDLE_SEC - 60

    assert client.get('/api/status').status_code == 401


def test_a_working_day_does_not_time_out(app):
    """The idle clock runs from the LAST REQUEST, not from the login:
    signing a trader out at 14:00 because they logged in at 06:00 would
    do it in the middle of a position."""
    client = signed_in(app)
    with client.session_transaction() as session:
        session['seen'] = time.time() - auth.IDLE_SEC + 60

    assert client.get('/api/status').status_code == 200
    # ...and that request moved the clock on.
    with client.session_transaction() as session:
        assert time.time() - session['seen'] < 5


def test_an_order_needs_the_page_that_placed_it(app, paths):
    """`/api/command` is the button that sends orders. A POST that
    arrives without this session's token is either a stale page or
    another site, and neither may trade."""
    client = signed_in(app)
    with open(paths['status'], 'w', encoding='utf-8') as f:
        json.dump({'at': time.time(), 'pairs': {}, 'accounts': {}}, f)

    refused = client.post('/api/command', json={'kind': 'flatten_pair'},
                          headers={'X-CSRF-Token': 'not-the-token'})

    assert refused.status_code == 403
    assert 'out of date' in refused.get_json()['error']
    # The control: the same command, with the token the page carries.
    allowed = client.post('/api/command', json={'kind': 'flatten_pair'})
    assert allowed.status_code == 200, allowed.get_json()


def test_a_write_with_no_token_at_all_is_refused(app):
    client = signed_in(app)
    client.csrf_token = None                     # a page issued none

    assert client.delete('/api/pairs/A%7CB').status_code == 403


def test_the_first_run_screen_makes_the_only_account(app, store):
    """The product ships with no account and no default password: one
    that ships with a password is one that runs with it."""
    client = app.test_client()
    assert b'Set up this terminal' in client.get('/login').data

    made = client.post('/setup', data={'username': 'Trader',
                                       'password': GOOD, 'confirm': GOOD,
                                       'csrf_token': csrf_of(client)})

    assert made.status_code == 302
    assert store.usernames() == ['trader']       # tidied, and signed in
    # ...and sent straight on to the authenticator, which is the rest of
    # the door: a password on its own does not open this screen.
    assert '/enrol' in client.get('/').headers['Location']
    # And the door closes behind it: setup is guarded on the STORE, so
    # the second caller cannot make a second account.
    other = app.test_client()
    second = other.post('/setup', data={'username': 'intruder',
                                        'password': GOOD, 'confirm': GOOD,
                                        'csrf_token': csrf_of(other)})
    assert second.status_code == 409
    assert store.usernames() == ['trader']


def test_setup_refuses_two_different_passwords(app, store):
    client = app.test_client()

    answer = client.post('/setup', data={'username': 'trader',
                                         'password': GOOD,
                                         'confirm': GOOD + 'x',
                                         'csrf_token': csrf_of(client)})

    assert answer.status_code == 400
    assert b'not the same' in answer.data
    assert store.usernames() == []


def test_the_login_page_says_why_it_refused(app, store):
    """Never "check the log": the person reading this owns the machine,
    and the reason they cannot get in is the whole message."""
    store.create_user('trader', GOOD)
    client = app.test_client()

    answer = client.post('/login', data={'username': 'trader',
                                         'password': 'wrong',
                                         'csrf_token': csrf_of(client)})

    assert answer.status_code == 401
    assert b'do not match' in answer.data
    assert b'attempts left' in answer.data


def test_signing_in_goes_where_the_browser_was_sent_from(app, store):
    store.create_user('trader', GOOD)
    client = app.test_client()

    answer = client.post('/login', data={'username': 'trader',
                                         'password': GOOD, 'next': '/?pair=x',
                                         'csrf_token': csrf_of(client)})

    assert answer.headers['Location'] == '/?pair=x'


def test_the_login_page_is_not_an_open_redirect(app, store):
    """A `next` that leaves this machine turns the sign-in screen into a
    way of sending a trader somewhere that looks like it."""
    store.create_user('trader', GOOD)
    for target in ('https://example.test/', '//example.test/', r'/\example'):
        client = app.test_client()
        answer = client.post('/login', data={'username': 'trader',
                                             'password': GOOD, 'next': target,
                                             'csrf_token': csrf_of(client)})
        assert answer.headers['Location'] == '/', target


def test_a_password_that_must_change_gets_no_further(app, store):
    """An account handed over with a password someone else knows must
    not be able to trade on it."""
    store.create_user('trader', GOOD, must_change=True)
    client = signed_in(app, password=GOOD)

    ladder = client.get('/')
    assert ladder.status_code == 302 and '/password' in ladder.headers['Location']
    assert client.post('/api/command', json={'kind': 'flatten_pair'}
                       ).status_code == 403

    changed = client.post('/password', data={'current': GOOD,
                                             'password': 'a-new-pass-2026',
                                             'confirm': 'a-new-pass-2026',
                                             'csrf_token': client.csrf_token})

    assert changed.status_code == 302
    with client.session_transaction() as session:
        client.csrf_token = session['csrf']
    assert client.get('/').status_code == 200
    assert store.authenticate('trader', 'a-new-pass-2026') == 'trader'


def test_changing_a_password_needs_the_current_one(app, store):
    """Otherwise a screen left open is a screen that can be taken over
    for good."""
    store.create_user('trader', GOOD)
    client = signed_in(app, password=GOOD)

    answer = client.post('/password', data={'current': 'not-it',
                                            'password': 'a-new-pass-2026',
                                            'confirm': 'a-new-pass-2026',
                                            'csrf_token': client.csrf_token})

    assert answer.status_code == 401
    assert store.authenticate('trader', GOOD) == 'trader'


def test_signing_out_ends_the_session(app):
    client = signed_in(app)

    client.post('/logout')

    assert client.get('/api/status').status_code == 401


def test_the_cookie_is_not_readable_by_script(app, store):
    """An injected script must not be able to lift the session and
    replay it — HttpOnly, and SameSite so no other site can post with
    it either."""
    assert app.config['SESSION_COOKIE_HTTPONLY'] is True
    assert app.config['SESSION_COOKIE_SAMESITE'] == 'Lax'


def test_the_login_page_leaks_nothing_about_the_account(app, store):
    """It is the one page an unauthenticated caller can read."""
    store.create_user('trader', GOOD)
    body = app.test_client().get('/login').data.decode()

    assert 'trader' not in body.lower().replace('spread terminal', '')
    assert GOOD not in body


def test_the_terminal_can_be_signed_out_of(app):
    """A terminal you cannot sign out of is one that stays signed in."""
    client = signed_in(app)
    body = client.get('/').data.decode()

    assert 'action="/logout"' in body
    assert 'trader' in body                      # and who is signed in


def test_the_page_carries_the_token_that_its_writes_need(app):
    """The screen adds the token to every write in one place. If the
    page stops carrying it, every button on the ladder stops working —
    so it is asserted here rather than found on a Tuesday."""
    body = signed_in(app).get('/').data.decode()

    assert 'name="csrf-token"' in body
    assert 'X-CSRF-Token' in body


# -- the second factor -----------------------------------------------------

def enrolled(store, username='trader', password=GOOD):
    """An account with a working authenticator. Returns its secret."""
    if username not in store.usernames():
        store.create_user(username, password)
    secret = store.start_enrolment(username)
    store.confirm_enrolment(username, auth.totp_code(secret))
    return secret


def next_code(secret):
    """The code the app will show next — enrolment spent this one."""
    return auth.totp_code(secret, at=time.time() + auth.TOTP_STEP)


def test_the_codes_are_the_ones_rfc_6238_specifies():
    """Checked against the RFC's own vectors, because "my phone says a
    different number" is not something to debug at 7am with a live
    position on."""
    import base64
    secret = base64.b32encode(b'12345678901234567890').decode().rstrip('=')

    assert auth.totp_code(secret, at=59) == '287082'
    assert auth.totp_code(secret, at=1111111109) == '081804'
    assert auth.totp_code(secret, at=1234567890) == '005924'


def test_a_code_from_the_step_before_still_works(store):
    """A trader who starts typing at 29 seconds past must not be told
    they are wrong — and a phone clock is never exactly ours."""
    secret = enrolled(store)
    now = time.time() + 2 * auth.TOTP_STEP

    assert store.check_second_factor(
        'trader', auth.totp_code(secret, at=now - auth.TOTP_STEP),
        now=now) == 'code'


def test_a_code_from_last_year_does_not(store):
    secret = enrolled(store)
    now = time.time()

    with pytest.raises(auth.AuthError):
        store.check_second_factor('trader',
                                  auth.totp_code(secret, at=now - 3600),
                                  now=now)


def test_a_code_works_once(store):
    """Shoulder-surfed, or read off a screen share: the thirty seconds
    it would otherwise stay valid for is thirty seconds too many."""
    secret = enrolled(store)
    now = time.time() + 5 * auth.TOTP_STEP
    code = auth.totp_code(secret, at=now)
    assert store.check_second_factor('trader', code, now=now) == 'code'

    with pytest.raises(auth.AuthError) as again:
        store.check_second_factor('trader', code, now=now + 1)

    assert 'already been used' in str(again.value)
    # ...and the control: the NEXT code is fine, so this is a replay
    # being refused and not the authenticator being broken.
    assert store.check_second_factor(
        'trader', auth.totp_code(secret, at=now + auth.TOTP_STEP),
        now=now + auth.TOTP_STEP) == 'code'


def test_a_recovery_code_signs_in_once(store, paths):
    """The day the phone is lost. There is no server to email a reset
    link from, so this is the whole of the answer."""
    store.create_user('trader', GOOD)
    secret = store.start_enrolment('trader')
    codes = store.confirm_enrolment('trader', auth.totp_code(secret))
    assert len(codes) == auth.RECOVERY_CODES

    assert store.check_second_factor('trader', codes[0]) == 'recovery'
    assert store.recovery_left('trader') == auth.RECOVERY_CODES - 1

    # Spent, whether or not the phone ever comes back.
    with pytest.raises(auth.AuthError):
        store.check_second_factor('trader', codes[0])
    # And the control: a different one still works.
    assert store.check_second_factor('trader', codes[1]) == 'recovery'


def test_the_recovery_codes_are_not_stored_in_the_clear(store, paths):
    """The file would otherwise be a list of ten ways in."""
    store.create_user('trader', GOOD)
    secret = store.start_enrolment('trader')
    codes = store.confirm_enrolment('trader', auth.totp_code(secret))

    raw = open(paths['auth'], encoding='utf-8').read()

    for code in codes:
        assert code not in raw
    assert store.check_second_factor('trader', codes[0]) == 'recovery'


def test_guessing_the_code_counts_against_the_same_lockout(store):
    """Guessing the password and guessing the code are the same attack.
    Letting the second start fresh would double the guesses on offer."""
    secret = enrolled(store)
    for _ in range(auth.MAX_FAILURES):
        with pytest.raises(auth.AuthError):
            store.check_second_factor('trader', '000000')

    with pytest.raises(auth.AuthError) as shut:
        store.check_second_factor('trader', next_code(secret))
    assert 'held shut for another' in str(shut.value)


def test_an_unconfirmed_enrolment_is_not_an_authenticator(store):
    """A secret nobody has proved they can read is an account nobody can
    get back into."""
    store.create_user('trader', GOOD)
    store.start_enrolment('trader')

    assert store.totp_state('trader') == 'pending'
    with pytest.raises(auth.AuthError):
        store.check_second_factor('trader', '000000')


def test_enrolment_refuses_a_code_the_phone_is_not_showing(store):
    store.create_user('trader', GOOD)
    store.start_enrolment('trader')

    with pytest.raises(auth.AuthError) as refusal:
        store.confirm_enrolment('trader', '000000')

    assert 'clock on the phone' in str(refusal.value)
    assert store.totp_state('trader') == 'pending'


# -- the second factor, through the door -----------------------------------

def test_the_password_alone_does_not_open_the_terminal(app, store):
    """The whole point. A password that has leaked is still only a
    password."""
    enrolled(store)
    client = app.test_client()

    answer = client.post('/login', data={'username': 'trader',
                                         'password': GOOD,
                                         'csrf_token': csrf_of(client)})

    assert answer.status_code == 302
    assert '/login/code' in answer.headers['Location']
    # And nothing is signed in yet: there is no session to hijack, only
    # a username sitting in a cookie.
    assert client.get('/api/status').status_code == 401


def test_the_code_finishes_the_sign_in(app, store):
    secret = enrolled(store)
    client = app.test_client()
    client.post('/login', data={'username': 'trader', 'password': GOOD,
                                'csrf_token': csrf_of(client)})

    done = client.post('/login/code', data={'code': next_code(secret),
                                            'csrf_token': csrf_of(client)})

    assert done.status_code == 302
    assert client.get('/api/status').status_code == 200


def test_a_half_finished_sign_in_does_not_wait_all_night(app, store):
    """The password has already been typed by then."""
    enrolled(store)
    client = app.test_client()
    client.post('/login', data={'username': 'trader', 'password': GOOD,
                                'csrf_token': csrf_of(client)})
    with client.session_transaction() as session:
        session['pending_at'] = time.time() - 3600

    answer = client.get('/login/code')

    assert answer.headers['Location'] == '/login'


def test_a_wrong_code_says_what_it_would_also_accept(app, store):
    """The trader whose phone is in the other room needs to be told,
    on the screen, that the codes they printed will do."""
    enrolled(store)
    client = app.test_client()
    client.post('/login', data={'username': 'trader', 'password': GOOD,
                                'csrf_token': csrf_of(client)})

    answer = client.post('/login/code', data={'code': '000000',
                                              'csrf_token': csrf_of(client)})

    assert answer.status_code == 401
    assert b'recovery codes' in answer.data


def test_an_account_with_no_authenticator_gets_no_further_than_enrolling(app):
    """A new account cannot trade until it has a second factor. Half of
    a login is not a login."""
    client = signed_in(app, totp=False)

    ladder = client.get('/')
    assert ladder.status_code == 302 and '/enrol' in ladder.headers['Location']
    assert client.post('/api/command', json={'kind': 'flatten_pair'}
                       ).status_code == 403


def test_enrolling_shows_a_qr_and_the_secret_to_type(app, store):
    """The QR is the way in; the typed secret is the way in when a phone
    held at the wrong angle will not scan it."""
    client = signed_in(app, totp=False)

    page = client.get('/enrol').data.decode()

    assert '<svg' in page                       # drawn here, not fetched
    assert 'otpauth://totp/' not in page or 'secret' in page
    secret = store.load_secret('trader')
    assert secret[:4] in page                   # grouped in fours


def test_enrolling_ends_with_the_recovery_codes_on_screen(app, store):
    """Shown once. What is on disk from here on is hashes, so there is
    no screen and no support line that can show them again."""
    client = signed_in(app, totp=False)
    client.get('/enrol')
    secret = store.load_secret('trader')

    page = client.post('/enrol', data={'code': auth.totp_code(secret),
                                       'csrf_token': client.csrf_token})

    assert page.status_code == 200
    body = page.data.decode()
    assert 'Save these recovery codes' in body
    shown = re.findall(r'<li>([0-9a-f]{8}-[0-9a-f]{8})</li>', body)
    assert len(shown) == auth.RECOVERY_CODES
    # ...and they are the ones that work.
    assert store.check_second_factor('trader', shown[0]) == 'recovery'
    assert store.totp_state('trader') == 'on'


def test_a_spent_recovery_code_is_said_out_loud(app, store):
    """Nine left is fine. One left is a week's notice that the terminal
    is about to become unopenable, and nobody will count them."""
    store.create_user('trader', GOOD)
    secret = store.start_enrolment('trader')
    codes = store.confirm_enrolment('trader', auth.totp_code(secret))
    client = app.test_client()
    client.post('/login', data={'username': 'trader', 'password': GOOD,
                                'csrf_token': csrf_of(client)})
    client.post('/login/code', data={'code': codes[0],
                                     'csrf_token': csrf_of(client)})

    body = client.get('/').data.decode()

    assert 'Signed in with a recovery code' in body
    assert f'{auth.RECOVERY_CODES - 1} are left' in body
    # Said once, not on every reload for the rest of the day.
    assert 'Signed in with a recovery code' not in client.get('/').data.decode()
