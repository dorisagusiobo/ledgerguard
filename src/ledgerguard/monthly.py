"""Persistent UTC-month reservations. Unreconciled reservations stay counted."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

import psycopg

from ledgerguard.audit import app_connection
from ledgerguard.gateway import GatewayBlocked, decimal_value


@dataclass(frozen=True)
class Reservation:
    month: date
    amount: Decimal


class MonthlyLedger:
    label = 'monthly'

    def __init__(self, conninfo, *, clock=None):
        self._conninfo = conninfo
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def reserve(self, amount) -> Reservation:
        amount = decimal_value(amount)
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError('Monthly ledger clock must be timezone-aware')
        month = now.astimezone(timezone.utc).date().replace(day=1)
        try:
            with app_connection(self._conninfo) as conn, conn.transaction():
                conn.execute('INSERT INTO monthly_spend (month) VALUES (%s) ON CONFLICT DO NOTHING', (month,))
                reserved, spent = conn.execute('''SELECT reserved, spent FROM monthly_spend
                    WHERE month=%s FOR UPDATE''', (month,)).fetchone()
                limit = conn.execute('SELECT monthly_limit FROM spend_limits WHERE singleton').fetchone()[0]
                if spent + reserved + amount > limit:
                    raise GatewayBlocked('max_cost_exceeded', scope='monthly')
                conn.execute('UPDATE monthly_spend SET reserved=reserved+%s WHERE month=%s', (amount, month))
        except psycopg.Error:
            raise OSError('Monthly budget database unavailable') from None
        return Reservation(month, amount)

    def reconcile(self, reservation: Reservation, actual):
        actual = decimal_value(actual)
        try:
            with app_connection(self._conninfo) as conn, conn.transaction():
                changed = conn.execute('''UPDATE monthly_spend SET reserved=reserved-%s, spent=spent+%s
                    WHERE month=%s''', (reservation.amount, actual, reservation.month))
                if changed.rowcount != 1:
                    raise OSError('Monthly reservation month is missing')
        except psycopg.Error:
            raise OSError('Monthly budget reconciliation failed; reservation remains counted') from None
