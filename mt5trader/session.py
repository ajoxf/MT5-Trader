"""The session cutoff: what happens to orders and positions at 16:55.

One time, configured once, governing two different things:

- **Working orders** are governed by DAY / GTC (spec §3.1). A DAY order
  is cancelled at the cutoff; a GTC order lives until the trader
  cancels it — or until this system stops, which is the honest caveat
  the UI carries beside the selector.
- **Positions** are governed by ALLOW / EXIT_IF_PROFIT / EXIT_ALWAYS
  (spec §3.2), per ladder, defaulting to ALLOW. This is a CARRY
  decision, not a risk rule: holding a rich basis over the swap is
  often the whole trade.

`EXIT_IF_PROFIT` reads NET P&L — marked at the CLOSING touch, less
commission only. Marked at the mid it would flatten trades that are not
actually in profit.

An overnight close is urgent: market, by ticket, never resting. And it
reads no price level, so the staleness and jump guards do not withhold
it — a guard may withhold an order; it must never prevent a close.
"""

from datetime import datetime

from .models import OvernightMode, TimeInForce


def past_cutoff(now, close_hour, close_minute):
    """Is `now` at or past today's session cutoff?

    `now` is the BROKER-session local time; the caller converts, so this
    stays a pure comparison with nothing to get wrong about clocks.
    """
    cutoff = now.replace(hour=int(close_hour), minute=int(close_minute),
                         second=0, microsecond=0)
    return now >= cutoff


def overnight_action(mode, net_pnl, now, close_hour, close_minute):
    """'OVERNIGHT_CLOSE' or None, for one position at one moment."""
    mode = OvernightMode(getattr(mode, 'value', mode) or 'ALLOW')
    if mode is OvernightMode.ALLOW:
        return None
    if not past_cutoff(now, close_hour, close_minute):
        return None
    if mode is OvernightMode.EXIT_ALWAYS:
        return 'OVERNIGHT_CLOSE'
    # EXIT_IF_PROFIT: unmeasured P&L is NOT a profit. A position whose
    # mark could not be taken is left alone rather than flattened on a
    # number nobody has.
    if net_pnl is not None and net_pnl > 0:
        return 'OVERNIGHT_CLOSE'
    return None


class SessionClock:
    """Fires the cutoff ONCE a day, per pair.

    Once, because a rule that re-fires every poll after 16:55 would
    cancel a working order the trader deliberately placed at 16:56 — and
    would keep trying to flatten a position whose close failed.
    """

    def __init__(self, config, now=datetime.now):
        self.config = config
        self.now = now
        self._fired = {}                # pair key -> date it last fired

    def due(self, pair_key):
        now = self.now()
        if not past_cutoff(now, self.config.get('OVERNIGHT_CLOSE_HOUR', 16),
                           self.config.get('OVERNIGHT_CLOSE_MINUTE', 55)):
            return False
        return self._fired.get(pair_key) != now.date()

    def mark(self, pair_key):
        self._fired[pair_key] = self.now().date()


def day_orders(orders):
    """The working orders the cutoff cancels — DAY only."""
    return [o for o in orders if o.time_in_force is TimeInForce.DAY]


def gtc_caveat():
    """What GTC actually means here. On the screen, not only in the code.

    Nothing at the broker knows what a spread is, so a synthetic order
    that "survived" a restart would be a promise nothing could keep.
    """
    return ('GTC: until cancelled, or until this system stops — a '
            'synthetic order lives in this process, and nothing watches '
            'the spread while it is down')
