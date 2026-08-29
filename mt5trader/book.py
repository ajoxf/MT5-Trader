"""What is working and what is on — the coordinator's own books.

Three clicks at 58.40 are three orders of 1, not one order of 3, and
each must be individually cancellable. That is the rule this module
exists to keep: synthetics are tracked one per click, and any
aggregation (into a single real pending, or into a Work-column number)
is a VIEW over them, computed here, never a replacement for them.
"""

from collections import defaultdict

from .models import (OrderState, SpreadSide, SyntheticOrder)


class Book:
    def __init__(self):
        self._orders = {}                 # order_id -> SyntheticOrder
        self._positions = {}              # position_id -> SpreadPosition
        #: Our own fills, per pair and level — the ladder's LTQ column.
        #: MT5 gives us no spread tape, so this is all there is, and the
        #: UI says so rather than implying a market print.
        self.prints = defaultdict(list)

    # -- working orders ---------------------------------------------------

    def add_order(self, pair, side, level, quantity, order_type=None,
                  time_in_force=None, position_id=None):
        order = SyntheticOrder(
            pair.key, side, level, quantity,
            order_type or pair.order_type, time_in_force or pair.time_in_force,
            position_id=position_id)
        self._orders[order.order_id] = order
        return order

    def orders_for_position(self, position_id, working_only=True):
        """The closing orders armed against one position.

        An auto-TP left resting after its position is gone is the
        orphan-pending incident with a GUARANTEED fill: it executes,
        and with nothing to close it opens a naked position instead.
        """
        return [o for o in self._orders.values()
                if o.position_id == position_id
                and (o.is_working or not working_only)]

    def orders(self, pair_key=None, working_only=True):
        return [o for o in self._orders.values()
                if (pair_key is None or o.pair_key == pair_key)
                and (o.is_working or not working_only)]

    def order(self, order_id):
        return self._orders.get(order_id)

    def cancel(self, order_id, reason='cancelled by trader'):
        """Pull ONE synthetic. Returns it, or None if it is not working."""
        order = self._orders.get(order_id)
        if order is None or not order.is_working:
            return None
        order.state = OrderState.CANCELLED
        order.reason = reason
        return order

    def cancel_where(self, pair_key=None, side=None,
                     reason='cancelled by trader'):
        """`CXL B` / `CXL S` / `CXL All`, and the global kill.

        Returns the orders actually pulled, so the button can report a
        count instead of claiming success over an empty set.
        """
        if side is not None:
            side = SpreadSide(getattr(side, 'value', side))
        pulled = []
        for order in self.orders(pair_key):
            if side is not None and order.side is not side:
                continue
            if self.cancel(order.order_id, reason):
                pulled.append(order)
        return pulled

    def working_at(self, pair_key, level, tolerance=1e-9):
        """The Work column's number for one row: (buy qty, sell qty).

        Aggregated for display only — `orders()` still holds each click
        separately, and cancelling the cell pulls exactly one of them.
        """
        buys = sells = 0.0
        for order in self.orders(pair_key):
            if abs(order.level - level) > tolerance:
                continue
            if order.side is SpreadSide.BUY:
                buys += order.remaining
            else:
                sells += order.remaining
        return buys, sells

    def working_counts(self, pair_key=None):
        """(buys, sells) — the superscripts on the CXL buttons, so the
        trader can see a button will do something before pressing it."""
        buys = sum(1 for o in self.orders(pair_key)
                   if o.side is SpreadSide.BUY)
        sells = sum(1 for o in self.orders(pair_key)
                    if o.side is SpreadSide.SELL)
        return buys, sells

    # -- positions ---------------------------------------------------------

    def add_position(self, position):
        self._positions[position.position_id] = position
        if position.entry_spread is not None:
            self.prints[position.pair_key].append(
                {'level': position.entry_spread, 'quantity': position.quantity,
                 'side': position.side.value, 'at': position.opened_at})
        return position

    def positions(self, pair_key=None, open_only=True):
        return [p for p in self._positions.values()
                if (pair_key is None or p.pair_key == pair_key)
                and (p.is_open or not open_only)]

    def position(self, position_id):
        return self._positions.get(position_id)

    def net_position(self, pair_key):
        """(net spreads, average entry spread) for one ladder.

        Signed: buys positive. The average is weighted by quantity and
        anchored on executed fills; it is None when nothing is on, never
        0.0 — an average of no trades is not zero (spec §11).
        """
        net = 0.0
        weighted = 0.0
        volume = 0.0
        for position in self.positions(pair_key):
            signed = position.quantity if position.side is SpreadSide.BUY \
                else -position.quantity
            net += signed
            if position.entry_spread is not None:
                weighted += position.entry_spread * position.quantity
                volume += position.quantity
        return net, (weighted / volume if volume else None)

    def last_print(self, pair_key):
        prints = self.prints.get(pair_key) or []
        return prints[-1] if prints else None
