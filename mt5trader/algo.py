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

One algo, per ladder, and NONE by default:

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

A z-score algo was started here and has been TAKEN BACK OUT, on
purpose. It needs a specification before it needs code: what it
measures, how long it warms for, which gates hold it back, and — the
part that decides everything else — whether it ever places an order.
Half of it in the tree is worse than none of it, because the half that
exists is the half that looks finished.
"""

#: What can be selected, per ladder. Exactly one, and NONE is the
#: default — an algo nobody asked for is an algo nobody is watching.
NONE = 'NONE'
FAIR_SPREAD = 'FAIR_SPREAD'
ALGOS = (NONE, FAIR_SPREAD)

#: What kind of pair this is, which decides the fair-value arithmetic.
SPOT_FUTURE = 'SPOT_FUTURE'
FUTURE_FUTURE = 'FUTURE_FUTURE'
RELATED = 'RELATED'


def pair_kind(pair, expiry_a=None, expiry_b=None):
    """Spot vs future, calendar, or two different instruments.

    The OPERATOR declares it. This used to be inferred — both legs
    carry an expiry, so call it a calendar — and that is wrong in the
    one case it matters: UKOILV6 against USOILV6 is two futures with
    two dates and NO carry between them, because Brent and WTI are
    different oil. A calendar is the same underlying in two months,
    and nothing in a symbol's expiry says whether two contracts share
    an underlying.

    So the declaration is authoritative and the dates never change it.
    A pair declared RELATED has no fair spread however many expiries
    its legs report.
    """
    said = (getattr(pair, 'pair_type', None) or SPOT_FUTURE).upper()
    return said if said in (SPOT_FUTURE, FUTURE_FUTURE, RELATED) else RELATED


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
