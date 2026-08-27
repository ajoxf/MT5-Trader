"""The launcher: one command, every process.

    python start.py --config config.json

Spawns a leg runner per configured account, then the coordinator, and
restarts a crashed child with backoff. It must NOT `terminate()` a
child that is running its own shutdown — the coordinator's shutdown
sweeps pendings at both brokers, and killing it half way there leaves
orders resting that nothing is watching.
"""

import argparse
import json
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


def main():
    parser = argparse.ArgumentParser(description='MT5-Trader launcher')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    args = parser.parse_args()

    problems = check_config(args.config)
    if problems:
        print('Refusing to start:')
        for problem in problems:
            print(f'  - {problem}')
        sys.exit(1)

    config = TraderConfig.from_file(args.config)
    children = [
        Child(f'leg:{name}',
              [sys.executable, 'run_leg.py', '--config', args.config,
               '--account', name])
        for name, account in config.accounts.items() if account.endpoint]
    if not children:
        print('No account has an endpoint. Give each one its own '
              '(127.0.0.1:9101, 127.0.0.1:9102) and try again.')
        sys.exit(1)

    for child in children:
        child.start()
    # The coordinator last: it connects to the runners, and connecting
    # to one that has not bound its port yet is a retry loop nobody
    # needs to watch.
    time.sleep(2.0)
    coordinator = Child('coordinator',
                        [sys.executable, 'run_coordinator.py',
                         '--config', args.config, '--status', args.status])
    coordinator.start()
    children.append(coordinator)

    try:
        while True:
            for child in children:
                child.restart_if_dead()
            time.sleep(1.0)
    except KeyboardInterrupt:
        print('\n[launcher] stopping — the coordinator sweeps its pendings '
              'first, so give it a moment')
        # Coordinator first: it needs its runners alive to cancel through
        # them.
        for child in reversed(children):
            child.stop()


if __name__ == '__main__':
    main()
