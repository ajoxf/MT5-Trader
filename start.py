"""The launcher. One file, no arguments:

    py -3.11 start.py

It brings up everything: a leg runner per account, the coordinator, and
the web UI. Three things make it the only file anyone has to run.

**It starts the UI FIRST, even with nothing configured.** A fresh
install has no accounts, and the accounts are entered ON THE SCREEN —
credentials included, into `.env`, never into a file anyone edits by
hand. A launcher that exits with "no accounts configured" leaves the
operator with nowhere to enter them.

**It watches the config and restarts what changed.** Accounts, symbols
and the hedge ratio are read at startup by the runners and the
coordinator, so when the operator saves an account the engine has to
come back to see it. It does that itself, within a few seconds, and
says so — rather than making a restart the operator's job to remember.

**It never `terminate()`s a child that is running its own shutdown.**
The coordinator's shutdown sweeps pendings at both brokers; killing it
half way there leaves orders resting that nothing is watching.
"""

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from mt5trader.config import TraderConfig, endpoint_clash, login_clash, \
    terminal_clash
from mt5trader.ipc import parse_endpoint

#: How long a child gets to finish its own shutdown before it is killed.
#: The coordinator's sweep is two round trips per account, so this is
#: generous on purpose.
SHUTDOWN_GRACE_SEC = 30.0
BACKOFF_START_SEC = 1.0
BACKOFF_MAX_SEC = 30.0


def first_run(config_path):
    """Make the files a first run needs, and say what happened.

    A fresh clone has `config.example.json` and `.env.example` and
    nothing else. Copying them here means the operator never has to
    know that, and never has to open a text editor to start.
    """
    made = []
    if not os.path.exists(config_path):
        example = 'config.example.json'
        if os.path.exists(example):
            shutil.copyfile(example, config_path)
        else:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump({'accounts': {}, 'pairs': {}, 'settings': {}}, f,
                          indent=2)
        made.append(config_path)
    env = os.path.join(os.path.dirname(os.path.abspath(config_path)) or '.',
                       '.env')
    if not os.path.exists(env):
        if os.path.exists('.env.example'):
            shutil.copyfile('.env.example', env)
        else:
            open(env, 'a').close()
        try:
            os.chmod(env, 0o600)         # passwords live here
        except OSError:
            pass
        made.append(env)
    return made


def engine_fingerprint(config_path):
    """What the RUNNERS and the coordinator read at startup.

    Only these fields: a change to any of them means the engine is
    running on stale information and has to come back. Everything else
    the UI applies live, and restarting for it would interrupt trading
    for no reason.
    """
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None                      # mid-write; try again next pass
    accounts = {
        name: {key: (account or {}).get(key)
               for key in ('endpoint', 'login', 'server', 'terminal_path',
                           'password_env')}
        for name, account in (raw.get('accounts') or {}).items()}
    pairs = {
        key: {field: (pair or {}).get(field)
              for field in ('leg_a', 'leg_b', 'hedge_ratio', 'enabled')}
        for key, pair in (raw.get('pairs') or {}).items()}
    return json.dumps({'accounts': accounts, 'pairs': pairs}, sort_keys=True)


def check_config(path):
    """Refuse to start on a clash, and say which field to correct.

    Every one of these is also refused at SAVE time, in the UI. This is
    the backstop for a config edited by hand.
    """
    raw = json.load(open(path, encoding='utf-8')) if path else {}
    problems = []
    for name, account in (raw.get('accounts') or {}).items():
        account = account or {}
        endpoint = (account.get('endpoint') or '').strip()
        for clash in (endpoint_clash(raw, name, endpoint),
                      login_clash(raw, name, account.get('login')),
                      terminal_clash(raw, name, account.get('terminal_path'))):
            if clash:
                problems.append(f"account '{name}': {clash}")
        if endpoint:
            try:
                parse_endpoint(endpoint)
            except ValueError as e:
                problems.append(f"account '{name}': {e}")
    return problems


class Child:
    def __init__(self, name, argv):
        self.name = name
        self.argv = argv
        self.process = None
        self.backoff = BACKOFF_START_SEC
        self.started_at = None

    def start(self):
        self.process = subprocess.Popen(self.argv)
        self.started_at = time.time()
        print(f'[launcher] started {self.name} (pid {self.process.pid})')

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def restart_if_dead(self):
        if self.alive():
            # A child that has stayed up is not in trouble any more, so
            # the next crash starts from a short wait again.
            if time.time() - self.started_at > 60:
                self.backoff = BACKOFF_START_SEC
            return False
        code = self.process.returncode if self.process else None
        print(f'[launcher] {self.name} exited ({code}) — restarting in '
              f'{self.backoff:g}s')
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX_SEC)
        self.start()
        return True

    def stop(self):
        """Ask, then wait, and only then insist."""
        if not self.alive():
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=SHUTDOWN_GRACE_SEC)
        except subprocess.TimeoutExpired:
            print(f'[launcher] {self.name} did not finish its shutdown in '
                  f'{SHUTDOWN_GRACE_SEC:g}s — killing it. Check both '
                  f'terminals for pendings of ours still resting.')
            self.process.kill()


def port_in_use(host, port):
    """Is something already listening there?

    Almost always the answer is "the last MT5-Trader, still running".
    Windows keeps that process alive when its console is closed the
    wrong way, and the browser then goes on being served by the OLD
    one — old page, old engine, and a pull that appears to do nothing
    however many times it is run. That is a miserable thing to
    diagnose from a screenshot, so it is refused here with the fix.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex((host if host != '0.0.0.0' else '127.0.0.1',
                                 int(port))) == 0
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser(description='MT5-Trader launcher')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--db', default='mt5trader.db')
    parser.add_argument('--web-port', type=int, default=8000)
    parser.add_argument('--host', default='127.0.0.1',
                        help='the UI has no password — leave this on '
                             'localhost and reach it through a tunnel')
    parser.add_argument('--no-web', action='store_true',
                        help='engine only, no browser UI')
    parser.add_argument('--no-browser', action='store_true',
                        help='do not open a browser window')
    args = parser.parse_args()

    made = first_run(args.config)
    for path in made:
        print(f'[launcher] created {path}')

    if not args.no_web and port_in_use(args.host, args.web_port):
        print(f'\n[launcher] PORT {args.web_port} IS ALREADY IN USE.\n'
              f'  Another MT5-Trader is almost certainly still running — '
              f'and your browser is being served by THAT one, which is why '
              f'a pull can look like it did nothing.\n'
              f'  Close the other black window (or end its python.exe in '
              f'Task Manager), then start this again.\n'
              f'  To run a second copy deliberately: '
              f'python start.py --web-port {args.web_port + 1}\n')
        return 1

    url = f'http://127.0.0.1:{args.web_port}/'
    web = None
    if not args.no_web:
        # FIRST, and before anything can refuse to start: the accounts
        # are entered on this page, so it has to be reachable even when
        # there are none.
        web = Child('web', [sys.executable, '-m', 'mt5trader.webapp',
                            '--config', args.config, '--status', args.status,
                            '--commands', args.commands,
                            '--results', args.results, '--db', args.db,
                            '--host', args.host,
                            '--port', str(args.web_port)])
        web.start()
        print(f'[launcher] the screen is at {url}')
        if not args.no_browser:
            time.sleep(2.0)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception as e:                 # a headless box
                print(f'[launcher] could not open a browser ({e}) — '
                      f'browse to {url}')

    engine = Engine(args)
    fingerprint = None
    try:
        while True:
            if web is not None:
                web.restart_if_dead()

            problems = check_config(args.config)
            current = engine_fingerprint(args.config)
            if problems:
                # Refuse to run the ENGINE on a clashing config, but
                # keep the screen up: the page that fixes it is the one
                # being served.
                engine.stop('the configuration has a clash')
                for problem in problems:
                    print(f'[launcher] {problem}')
                print('[launcher] fix it on the Exchanges page; the engine '
                      'starts itself when it is right')
                fingerprint = None
                time.sleep(3.0)
                continue

            if current is not None and current != fingerprint:
                if fingerprint is not None:
                    print('[launcher] the accounts or pairs changed — '
                          'restarting the engine to pick it up')
                fingerprint = current
                engine.restart()
                if not engine.children:
                    print('[launcher] no account has a runner endpoint yet. '
                          f'Add your accounts on the Exchanges page at {url} '
                          f'— the engine starts itself as soon as one is '
                          f'saved.')

            engine.supervise()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print('\n[launcher] stopping — the coordinator sweeps its pendings '
              'first, so give it a moment')
        engine.stop('the launcher was stopped')
        if web is not None:
            web.stop()


class Engine:
    """The processes that talk to MT5: the runners and the coordinator.

    Kept together because they come and go together: a runner restarted
    without the coordinator leaves the coordinator holding a dead
    socket, and the coordinator restarted alone re-reads a config the
    runners have not.
    """

    def __init__(self, args):
        self.args = args
        self.children = []

    def restart(self):
        self.stop('picking up a configuration change')
        config = TraderConfig.from_file(self.args.config)
        # A port still held is a runner from the last start, still
        # attached to the terminal. Two clients logging one terminal in
        # is a feed that ticks for a few seconds and then goes quiet —
        # so it is named here rather than left to look like a broken
        # market.
        for name, account in config.accounts.items():
            if not account.endpoint:
                continue
            try:
                host, port = parse_endpoint(account.endpoint)
            except ValueError:
                continue
            if port_in_use(host, port):
                print(f"[launcher] account '{name}': {account.endpoint} is "
                      f"ALREADY IN USE. A leg runner from a previous start "
                      f"is still there — close the other window or end its "
                      f"python.exe, or this account will have two clients "
                      f"on one terminal and a feed that keeps dropping.")
        self.children = [
            Child(f'leg:{name}',
                  [sys.executable, 'run_leg.py',
                   '--config', self.args.config, '--account', name])
            for name, account in config.accounts.items() if account.endpoint]
        if not self.children:
            return
        for child in self.children:
            child.start()
        # The coordinator last: connecting to a runner that has not
        # bound its port yet is a retry loop nobody needs to watch.
        time.sleep(2.0)
        coordinator = Child('coordinator',
                            [sys.executable, 'run_coordinator.py',
                             '--config', self.args.config,
                             '--status', self.args.status,
                             '--commands', self.args.commands,
                             '--results', self.args.results,
                             '--db', self.args.db])
        coordinator.start()
        self.children.append(coordinator)

    def supervise(self):
        for child in self.children:
            child.restart_if_dead()

    def stop(self, why):
        if not self.children:
            return
        print(f'[launcher] stopping the engine — {why}')
        # Coordinator first: it needs its runners alive to cancel
        # through them.
        for child in reversed(self.children):
            child.stop()
        self.children = []


if __name__ == '__main__':
    raise SystemExit(main() or 0)
