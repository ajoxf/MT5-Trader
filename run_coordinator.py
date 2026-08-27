"""Start the coordinator: two feeds in, one spread per pair out.

    python run_coordinator.py --config config.json

Connects to each account's leg runner, fuses their ticks into a spread
per configured pair, and publishes one status snapshot the ladders, the
Market Grid and the positions monitor all render from.
"""

import argparse
import logging
import signal
import sys

from mt5trader.commands import CommandRunner
from mt5trader.config import TraderConfig
from mt5trader.coordinator import Coordinator
from mt5trader.legs import RemoteLeg
from mt5trader.shutdown import should_close


def build_legs(config):
    """One RemoteLeg per account with an endpoint.

    An account without one has no runner: it cannot serve a leg, and
    saying so here beats a connection error three layers down.
    """
    legs = {}
    for name, account in config.accounts.items():
        if not account.endpoint:
            logging.error(
                "account '%s' has no endpoint — give it one (e.g. "
                "127.0.0.1:9101) and start its leg runner", name)
            continue
        leg = RemoteLeg(name, account.endpoint)
        if leg.connect():
            legs[name] = leg
    return legs


def main():
    parser = argparse.ArgumentParser(description='MT5-Trader coordinator')
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - [coord] %(message)s',
        handlers=[logging.FileHandler('coordinator.log', encoding='utf-8'),
                  logging.StreamHandler()])

    config = TraderConfig.from_file(args.config)
    legs = build_legs(config)
    if not legs:
        print('No leg runners reachable. Start them first:\n'
              '    python run_leg.py --config config.json --account <name>')
        sys.exit(1)

    coordinator = Coordinator(config, legs, status_path=args.status)
    # PRIME before the first drain. Everything already in the command
    # file was written to a process that is gone; replaying it would
    # place those orders again, now, at today's prices.
    runner = CommandRunner(coordinator, args.commands, args.results)
    runner.prime()
    coordinator.commands = runner

    def stop(signum, frame):
        # Positions first, because closing them is irreversible and an
        # unanswered prompt must mean NO (spec §12). The pending sweep
        # runs either way — a pending of ours left resting can fill
        # unhedged with nobody watching.
        open_positions = coordinator.book.positions()
        if should_close(open_positions,
                        config.get('SHUTDOWN_CLOSE_POSITIONS', 'ask')):
            for position in open_positions:
                coordinator.executor.close_position(
                    config.pairs[position.pair_key], position,
                    coordinator.market.get(position.pair_key),
                    reason='shutdown')
        report = coordinator.stop()
        if report['failed'] or report['unknown']:
            logging.critical('SHUTDOWN SWEEP INCOMPLETE: %s', report)
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    coordinator.run()


if __name__ == '__main__':
    main()
