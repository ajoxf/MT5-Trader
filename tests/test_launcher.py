"""The launcher: one file, no arguments, and nothing to edit by hand.

The rules here exist because the operator is not the person who built
this: a fresh clone must start, the screen must come up even with
nothing configured, and saving an account on that screen must be enough
to get the engine running.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import start                                                    # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_fresh_clone_makes_the_files_it_needs(workdir):
    """Nobody should have to know that config.example.json exists."""
    (workdir / 'config.example.json').write_text(
        json.dumps({'accounts': {}, 'pairs': {'X|Y': {}}}), encoding='utf-8')
    (workdir / '.env.example').write_text('MT5_PASSWORD_A=""\n',
                                          encoding='utf-8')

    made = start.first_run('config.json')

    assert (workdir / 'config.json').exists()
    assert (workdir / '.env').exists()
    assert len(made) == 2
    assert json.loads((workdir / 'config.json').read_text())['pairs'] == {
        'X|Y': {}}
    # Second time: nothing to do, and nothing overwritten.
    (workdir / 'config.json').write_text('{"accounts": {"a": {}}}',
                                         encoding='utf-8')
    assert start.first_run('config.json') == []
    assert 'a' in json.loads((workdir / 'config.json').read_text())['accounts']


def test_it_starts_even_with_no_example_to_copy(workdir):
    made = start.first_run('config.json')
    assert made == ['config.json', str(workdir / '.env')]
    assert json.loads((workdir / 'config.json').read_text()) == {
        'accounts': {}, 'pairs': {}, 'settings': {}}


def test_the_env_file_is_not_world_readable(workdir):
    """It holds the passwords, and nothing else does."""
    start.first_run('config.json')
    mode = os.stat(workdir / '.env').st_mode & 0o777
    assert mode == 0o600 or os.name == 'nt'


def test_only_what_the_engine_reads_at_startup_triggers_a_restart(workdir):
    """Restarting for a display setting would interrupt trading for no
    reason; not restarting for a symbol change would leave the engine
    trading the wrong instrument."""
    config = {
        'accounts': {'a': {'endpoint': '127.0.0.1:9101', 'login': 1}},
        'pairs': {'X|Y': {'leg_a': {'account': 'a', 'symbol': 'X'},
                          'hedge_ratio': 1.0, 'enabled': True,
                          'increment': 0.01}},
        'settings': {'MARKET_PROTECTION_TICKS': 3.0},
    }
    (workdir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    before = start.engine_fingerprint('config.json')

    # Comfort settings and per-ladder knobs: no restart.
    config['settings']['MARKET_PROTECTION_TICKS'] = 5.0
    config['pairs']['X|Y']['increment'] = 0.05
    (workdir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    assert start.engine_fingerprint('config.json') == before

    # A symbol is structural: the runners read it at startup.
    config['pairs']['X|Y']['leg_a']['symbol'] = 'Z'
    (workdir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    assert start.engine_fingerprint('config.json') != before


def test_a_new_account_is_a_restart_because_the_engine_cannot_see_it(workdir):
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {}, 'pairs': {}}), encoding='utf-8')
    empty = start.engine_fingerprint('config.json')

    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {'a': {'endpoint': '127.0.0.1:9101'}},
                    'pairs': {}}), encoding='utf-8')

    assert start.engine_fingerprint('config.json') != empty


def test_a_half_written_config_is_not_read_as_a_change(workdir):
    """The UI writes through a tmp file, but a reader can still catch a
    filesystem mid-flight. A restart on a partial read would be a
    restart for nothing."""
    (workdir / 'config.json').write_text('{"accounts": ', encoding='utf-8')
    assert start.engine_fingerprint('config.json') is None


def test_the_launcher_refuses_to_run_an_engine_on_a_clashing_config(workdir):
    """Two accounts on one port is the same MT5 account twice. The
    screen stays up so the clash can be fixed on it."""
    (workdir / 'config.json').write_text(json.dumps({
        'accounts': {'a': {'endpoint': '127.0.0.1:9101'},
                     'b': {'endpoint': '127.0.0.1:9101'}},
        'pairs': {}}), encoding='utf-8')

    problems = start.check_config('config.json')

    assert problems and 'already belongs to account' in problems[0]


def test_a_config_with_no_accounts_is_not_a_problem_to_refuse(workdir):
    """A fresh install has none — that is where every install starts,
    and the answer is to open the page, not to exit."""
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {}, 'pairs': {}}), encoding='utf-8')
    assert start.check_config('config.json') == []


class FakeChild:
    """Records what the launcher would have spawned."""

    started = []
    stopped = []

    def __init__(self, name, argv):
        self.name = name
        self.argv = argv
        self.alive_ = False

    def start(self):
        self.alive_ = True
        FakeChild.started.append(self.name)

    def alive(self):
        return self.alive_

    def restart_if_dead(self):
        return False

    def stop(self):
        self.alive_ = False
        FakeChild.stopped.append(self.name)


def test_the_engine_spawns_a_runner_per_account_and_one_coordinator(
        workdir, monkeypatch):
    (workdir / 'config.json').write_text(json.dumps({
        'accounts': {'spot': {'endpoint': '127.0.0.1:9101'},
                     'fut': {'endpoint': '127.0.0.1:9102'},
                     'no-runner': {}},
        'pairs': {}}), encoding='utf-8')
    FakeChild.started, FakeChild.stopped = [], []
    monkeypatch.setattr(start, 'Child', FakeChild)
    monkeypatch.setattr(start.time, 'sleep', lambda seconds: None)
    engine = start.Engine(_args(workdir))

    engine.restart()

    assert FakeChild.started == ['leg:spot', 'leg:fut', 'coordinator']
    # An account with no endpoint has no runner — and is not an error:
    # it is an account that has not been given a port yet.
    assert 'leg:no-runner' not in FakeChild.started

    engine.stop('test')
    # Coordinator FIRST: it needs its runners alive to cancel through
    # them.
    assert FakeChild.stopped == ['coordinator', 'leg:fut', 'leg:spot']


def test_no_endpoint_anywhere_starts_nothing_and_is_not_a_crash(workdir,
                                                                monkeypatch):
    (workdir / 'config.json').write_text(
        json.dumps({'accounts': {'a': {}}, 'pairs': {}}), encoding='utf-8')
    FakeChild.started = []
    monkeypatch.setattr(start, 'Child', FakeChild)
    engine = start.Engine(_args(workdir))

    engine.restart()

    assert engine.children == []
    assert FakeChild.started == []


def _args(workdir):
    import argparse
    return argparse.Namespace(
        config='config.json', status='status.json',
        commands='commands.jsonl', results='results.json', db='trader.db')


def test_a_second_launcher_refuses_the_port_instead_of_being_invisible(
        workdir, monkeypatch, capsys):
    """The failure this exists for: the last instance is still running,
    the browser goes on being served by THAT one, and a pull looks like
    it did nothing however many times it is run — old page, old engine,
    and nothing on screen to say so."""
    import socket
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert start.port_in_use('127.0.0.1', port) is True

        monkeypatch.setattr(sys, 'argv',
                            ['start.py', '--web-port', str(port)])
        code = start.main()

        assert code == 1
        printed = capsys.readouterr().out
        assert 'ALREADY IN USE' in printed
        assert 'Close the other black window' in printed
        # ...and the way to run a second one on purpose.
        assert f'--web-port {port + 1}' in printed
    finally:
        listener.close()

    # The control: a free port is not refused.
    assert start.port_in_use('127.0.0.1', port) is False
