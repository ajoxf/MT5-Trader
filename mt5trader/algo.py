"""The two algos, and the switch that says which one is running.

This system is a MANUAL ladder. Every rule it is built on says so, and
the two most important of them are worth repeating here because this
module is where they would be lost:

    No strategy and no loops. No signals, no automatic entries or
    exits, nothing that re-enters by itself.

So what follows is deliberately only half of an algo. It measures, and
it says what it would do. **It does not place, modify or cancel an
order, and nothing in this file touches the manual path** — a click on
the ladder behaves identically whether an algo is selected or not, and
whether it is screaming BUY or saying nothing at all.

Turning the other half on — letting one of these actually trade — is a
separate, deliberate step that has to be asked for. Reading a number
off a screen and deciding is the trader's job today.

Two algos, one at a time, per ladder:

**FAIR_SPREAD** — what the basis SHOULD be, on financing alone. It
first asks what kind of pair this is, because the answer changes the
arithmetic:

- *spot vs a future*: the future converges to the spot on ITS expiry,
  so the carry runs to that date;
- *future vs future* (a calendar): the near leg is the one that
  expires first and the spread is decided then, so the carry runs to
  the NEAR expiry;
- *two different instruments*: nothing forces them together and there
  is no fair value to quote. Saying so is the honest answer.

**STAT_ARB** — the spread against its own recent history: a rolling
mean, a standard deviation, and a z-score. Beyond the configured z it
says ENTER, and the level it would leave at is break-even plus the
profit percentage — the same exit arithmetic the manual panel uses,
not a second one.

The one thing about the statistics that is not obvious, and that cost
real money in the system this is ported from: **the window is fed by
quote EVENTS, not by polls**. The coordinator polls faster than either
broker ticks, so counting polls fills the window with the same quote
over and over; sigma collapses toward zero and z explodes. Live, that
produced a z of +53,026 on a spread of 9.13.
"""

import math
from collections import deque

#: What can be selected, per ladder. Exactly one, and NONE is the
#: default — an algo nobody asked for is an algo nobody is watching.
NONE = 'NONE'
FAIR_SPREAD = 'FAIR_SPREAD'
STAT_ARB = 'STAT_ARB'
ALGOS = (NONE, FAIR_SPREAD, STAT_ARB)

#: What kind of pair this is, which decides the fair-value arithmetic.
SPOT_FUTURE = 'SPOT_FUTURE'
FUTURE_FUTURE = 'FUTURE_FUTURE'
RELATED = 'RELATED'


def pair_kind(pair, expiry_a=None, expiry_b=None):
    """Spot vs future, calendar, or two different instruments.

    The configured `pair_type` is what the operator SAID; the expiries
    are what the contracts ARE. Where they disagree the expiries win
    for the arithmetic — a leg with a date is a contract that expires,
    whatever the pair was labelled — but only to narrow, never to
    invent a basis between two instruments that have none.
    """
    said = (getattr(pair, 'pair_type', None) or SPOT_FUTURE).upper()
    if said == RELATED:
        return RELATED
    has_a, has_b = bool(expiry_a), bool(expiry_b)
    if has_a and has_b:
        return FUTURE_FUTURE
    if has_b:
        return SPOT_FUTURE
    # Leg B with no date is not a future. Say what it is rather than
    # pricing a carry to a date nobody has given.
    return said if said in (SPOT_FUTURE, FUTURE_FUTURE) else RELATED


def carry_nights(kind, days_a, days_b):
    """How many nights the carry runs for, and why.

    A spread is decided on the day the FIRST of its legs converges: for
    spot vs a future that is the future's own expiry; for a calendar it
    is the near leg's. Running a calendar's carry to the far expiry
    prices a trade that is already over.
    """
    if kind == RELATED:
        return None, 'two different instruments — no date decides this pair'
    if kind == FUTURE_FUTURE:
        if days_a is None or days_b is None:
            return None, 'a calendar spread needs BOTH legs’ expiries'
        near = min(days_a, days_b)
        return near, (f'calendar spread — {near:g} night(s) to the NEAR '
                      f'expiry, which is when it is decided')
    if days_b is None:
        return None, "set the future’s expiry to price its carry"
    return days_b, f'spot vs a future — {days_b:g} night(s) to expiry'


class Series:
    """A rolling mean and standard deviation of ONE executable side.

    One side, not a midpoint: the spread has a bid and an ask, and a
    z-score measured on an average of the two is measured on a price
    nobody fills at. A buy is judged against what it would PAY and a
    sell against what it would RECEIVE.

    Fed by quote EVENTS. A repeated quote adds no sample, however many
    times the coordinator polls it — that is what keeps sigma a
    property of the market rather than of the poll rate.
    """

    def __init__(self, lookback_sec=1800.0, clock=None):
        import time as _time
        self.lookback_sec = float(lookback_sec or 0) or 1800.0
        self.clock = clock or _time.time
        self.samples = deque()          # (at, value)
        self.last_id = None
        self.mu = None
        self.sigma = None

    def observe(self, value, quote_id=None):
        """One quote. Returns True when it counted as a new sample."""
        now = self.clock()
        counted = False
        if value is not None and (quote_id is None or quote_id != self.last_id):
            self.last_id = quote_id
            self.samples.append((now, float(value)))
            counted = True
        # Ageing runs on EVERY call, sample or not: a feed that stops
        # ticking must drain out of the window and go cold, not freeze
        # a stale mean in place and keep quoting a z off it.
        horizon = now - self.lookback_sec
        while self.samples and self.samples[0][0] < horizon:
            self.samples.popleft()
        self._refresh()
        return counted

    def _refresh(self):
        if len(self.samples) < 2:
            self.mu = self.sigma = None
            return
        values = [value for _, value in self.samples]
        self.mu = sum(values) / len(values)
        # Sample standard deviation: with n in the denominator a short
        # window reads tighter than it is, and a z-score is exactly the
        # thing that then fires too early.
        variance = sum((v - self.mu) ** 2 for v in values) / (len(values) - 1)
        self.sigma = math.sqrt(variance)

    @property
    def ready(self):
        return self.mu is not None and (self.sigma or 0) > 0

    def z(self, value):
        """How many sigma `value` is from the mean, or None."""
        if value is None or not self.ready:
            return None
        return (value - self.mu) / self.sigma


class PairStats:
    """Both executable sides of one pair, kept side by side."""

    def __init__(self, lookback_sec=1800.0, clock=None):
        self.buy = Series(lookback_sec, clock)      # what a BUY would pay
        self.sell = Series(lookback_sec, clock)     # what a SELL receives

    def observe(self, md):
        if not md:
            return False
        # The quote's own identity, so a poll that saw nothing new adds
        # nothing. Both legs' tick times and both touches: any of them
        # moving is a new quote, and none of them moving is not.
        quote_id = (md.get('leg_a_tick_time'), md.get('leg_b_tick_time'),
                    md.get('leg_a_bid'), md.get('leg_a_ask'),
                    md.get('leg_b_bid'), md.get('leg_b_ask'))
        counted = self.buy.observe(md.get('long_spread'), quote_id)
        self.sell.observe(md.get('short_spread'), quote_id)
        return counted

    def resize(self, lookback_sec):
        for series in (self.buy, self.sell):
            series.lookback_sec = float(lookback_sec or 0) or 1800.0


def stat_arb(stats, md, entry_z=2.5, exit_levels=None):
    """The z-score readout, and what it WOULD do.

    `verdict` is a word on a screen. Nothing here places an order.
    """
    md = md or {}
    body = {'lookback_sec': stats.buy.lookback_sec if stats else None,
            'samples': len(stats.buy.samples) if stats else 0,
            'ready': bool(stats and stats.buy.ready and stats.sell.ready),
            'mu_buy': None, 'mu_sell': None,
            'sigma_buy': None, 'sigma_sell': None,
            'z_buy': None, 'z_sell': None,
            'entry_z': entry_z, 'verdict': 'NO DATA', 'note': None,
            'exit_level': None}
    if not stats:
        return body
    body.update({'mu_buy': stats.buy.mu, 'mu_sell': stats.sell.mu,
                 'sigma_buy': stats.buy.sigma, 'sigma_sell': stats.sell.sigma,
                 'z_buy': stats.buy.z(md.get('long_spread')),
                 'z_sell': stats.sell.z(md.get('short_spread'))})
    if not body['ready']:
        body['verdict'] = 'WARMING'
        body['note'] = (f"{body['samples']} quote(s) in a "
                        f"{body['lookback_sec']:g}s window — a z-score off "
                        f"two prices is not a z-score")
        return body

    threshold = abs(float(entry_z or 0))
    z_buy, z_sell = body['z_buy'], body['z_sell']
    # CHEAP: what a buy would pay is far BELOW its own mean.
    if z_buy is not None and z_buy <= -threshold:
        body['verdict'] = 'BUY'
    # RICH: what a sell would receive is far ABOVE its own mean.
    elif z_sell is not None and z_sell >= threshold:
        body['verdict'] = 'SELL'
    else:
        body['verdict'] = 'WAIT'
    if body['verdict'] in ('BUY', 'SELL') and exit_levels:
        body['exit_level'] = (exit_levels.get('tp_buy')
                              if body['verdict'] == 'BUY'
                              else exit_levels.get('tp_sell'))
    body['note'] = (f'|z| >= {threshold:g} on its own side enters; the exit is '
                    f'break-even plus the profit target, which is the same '
                    f'arithmetic the manual Exit box uses')
    return body
