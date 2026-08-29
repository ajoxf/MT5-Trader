"""A leg runner must not trade an account it was not configured for.

Live, twice: a leg runner whose own terminal was not up yet fell
through to "whatever terminal is already running", found the OTHER
leg's terminal, and traded that account. Both legs then hedged against
themselves on one login while every screen reported two.
"""

from mt5trader import diagnostics
from mt5trader.diagnostics import FAIL, PASS


def login_check(checklist):
    return [c for c in checklist.checks if c['name'] == 'Account login'][0]


def terminal(login):
    """A healthy terminal report — everything check_account needs to
    reach the login check, so the login is the only thing under test."""
    return {'library': True, 'terminal': True, 'path': r'C:\MT5-2',
            'running': True, 'connected': True, 'logged_in': True,
            'login': login, 'server': 'MentoMarkets-Server',
            'algo_trading': True, 'trade_allowed': True,
            'margin_mode': 'hedging'}


def test_diagnose_fails_when_the_terminal_is_on_another_account():
    """It reported whatever login it FOUND as a pass, so a leg attached
    to the other leg's terminal read "PASS — CONNECTED" while trading
    the wrong account."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100006),
                              expect_login=100002)

    check = login_check(checklist)
    assert check['status'] == FAIL
    assert '100006' in check['message'] and '100002' in check['message']


def test_diagnose_passes_when_the_terminal_is_on_the_right_account():
    """The CONTROL. A check that failed either way would only teach the
    operator to ignore it."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100002),
                              expect_login=100002)

    assert login_check(checklist)['status'] == PASS


def test_a_login_of_a_different_type_is_not_a_mismatch():
    """Config carries an int, MT5 answers with an int, and the webapp
    has handed both around as strings. Comparing them raw would fail a
    correctly configured account."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100002),
                              expect_login='100002')

    assert login_check(checklist)['status'] == PASS


def test_no_expected_login_still_passes():
    """Nothing configured is not a mismatch — an account may legitimately
    attach to whatever terminal is open."""
    checklist = diagnostics.Checklist()
    diagnostics.check_account(checklist, 'LegB', terminal(100006))

    assert login_check(checklist)['status'] == PASS


class FakeInfo:
    def __init__(self, login):
        self.login = login
        self.server = 'MentoMarkets-Server'
        self.name = 'Trader'


class FakeMT5:
    """One terminal, signed into `login`, that answers every attach."""

    def __init__(self, login):
        self.login = login
        self.attaches = 0

    def initialize(self, **kwargs):
        self.attaches += 1
        return True

    def account_info(self):
        return FakeInfo(self.login)

    def shutdown(self):
        pass

    def last_error(self):
        return (-2, 'no terminal')


def broker_for(monkeypatch, terminal_login, configured_login):
    from mt5trader import broker as broker_mod
    from mt5trader.config import AccountConfig
    fake = FakeMT5(terminal_login)
    monkeypatch.setattr(broker_mod, 'mt5', fake)
    account = AccountConfig('LegB', terminal_path=r'C:\MT5-2',
                            login=configured_login,
                            server='MentoMarkets-Server')
    return broker_mod.BrokerSession(account), fake


def test_the_runner_refuses_a_terminal_on_another_account(monkeypatch):
    """The whole failure, in one line: it used to WARN and connect, so
    the leg traded the other leg's account all session."""
    broker, fake = broker_for(monkeypatch, 100006, 100002)

    assert broker.initialize() is False
    assert broker.connected is False
    assert fake.attaches > 0, 'it never even tried — the test proves nothing'


def test_the_runner_connects_when_the_terminal_is_the_right_one(monkeypatch):
    """The CONTROL. A refusal that fired either way would just be a leg
    runner that never starts."""
    broker, _ = broker_for(monkeypatch, 100002, 100002)

    assert broker.initialize() is True
    assert broker.connected is True


def test_an_account_with_no_login_configured_still_attaches(monkeypatch):
    """A blank login means "attach to whatever is open", which is a
    supported way to run a single-account desk."""
    broker, _ = broker_for(monkeypatch, 100006, None)

    assert broker.initialize() is True
