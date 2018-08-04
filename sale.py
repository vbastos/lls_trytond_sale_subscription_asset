# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import datetime
from sql import Null
from sql.conditionals import Coalesce

from trytond.model import fields, ModelSQL, ModelView, Workflow
from trytond.pool import PoolMeta, Pool
from trytond.pyson import Eval, If, Bool
from trytond.tools import grouped_slice, reduce_ids
from trytond.transaction import Transaction


class SubscriptionService(metaclass=PoolMeta):
    __name__ = 'sale.subscription.service'

    asset_lots = fields.Many2Many(
        'sale.subscription.service-stock.lot.asset',
        'service', 'lot', "Asset Lots",
        domain=[
            ('product.type', '=', 'assets'),
            ])
    asset_lots_available = fields.Many2Many(
        'sale.subscription.service-stock.lot.asset',
        'service', 'lot', "Available Asset Lots", readonly=True,
        domain=[
            ('product.type', '=', 'assets'),
            ],
        filter=[
            ('subscribed', '=', None),
            ])


class SubscriptionServiceStockLot(ModelSQL):
    "Subscription Service - Stock Lot Asset"
    __name__ = 'sale.subscription.service-stock.lot.asset'

    service = fields.Many2One(
        'sale.subscription.service', "Service",
        ondelete='CASCADE', select=True, required=True)
    lot = fields.Many2One(
        'stock.lot', "Lot", ondelete='CASCADE', select=True, required=True,
        domain=[
            ('product.type', '=', 'assets'),
            ])


class Subscription(metaclass=PoolMeta):
    __name__ = 'sale.subscription'

    lines = fields.One2Many(
        'sale.subscription.line', 'subscription', "Lines",
        states={
            'readonly': ((Eval('state') != 'draft')
                | ~Eval('start_date')),
            },
        depends=['state'])

    @classmethod
    @ModelView.button
    @Workflow.transition('canceled')
    def cancel(cls, subscriptions):
        pool = Pool()
        SubscriptionLine = pool.get('sale.subscription.line')

        sub_lines = [l for s in subscriptions for l in s.lines if l.asset_lot]
        SubscriptionLine.write(sub_lines, {'asset_lot': None})

        super(Subscription, cls).cancel(subscriptions)

    @classmethod
    @ModelView.button
    @Workflow.transition('running')
    def run(cls, subscriptions):
        pool = Pool()
        Line = pool.get('sale.subscription.line')
        super(Subscription, cls).run(subscriptions)
        lines = [l for s in subscriptions for l in s.lines]
        Line._validate(lines, ['asset_lot'])


class SubscriptionLine(metaclass=PoolMeta):
    __name__ = 'sale.subscription.line'

    start_date = fields.Date(
        "Start Date", required=True,
        domain=[
            ('start_date', '>=', Eval('subscription_start_date')),
            ],
        states={
            'readonly': ((Eval('subscription_state') != 'draft')
                | Eval('consumed')),
            },
        depends=['subscription_start_date', 'subscription_state', 'consumed'])
    end_date = fields.Date(
        "End Date",
        domain=['OR', [
                ('end_date', '>=', Eval('start_date')),
                If(Bool(Eval('subscription_end_date')),
                    ('end_date', '<=', Eval('subscription_end_date')),
                    ()),
                If(Bool(Eval('next_consumption_date')),
                    ('end_date', '>=', Eval('next_consumption_date')),
                    ()),
                ],
            ('end_date', '=', None),
            ],
        states={
            'readonly': ((Eval('subscription_state') != 'draft')
                | (~Eval('next_consumption_date') & Eval('consumed'))),
            },
        depends=['subscription_end_date', 'start_date',
            'next_consumption_date', 'subscription_state', 'consumed'])
    asset_lot = fields.Many2One('stock.lot', "Asset Lot",
        domain=[
            ('subscription_services', '=', Eval('service')),
            ],
        states={
            'required': ((Eval('subscription_state') == 'running')
                & Eval('asset_lot_required')),
            'invisible': ~Eval('asset_lot_required'),
            'readonly': Eval('subscription_state') != 'draft',
            },
        depends=['service', 'subscription_state', 'asset_lot_required'])
    asset_lot_required = fields.Function(
        fields.Boolean("Asset Lot Required"),
        'on_change_with_asset_lot_required')

    @classmethod
    def __setup__(cls):
        super(SubscriptionLine, cls).__setup__()

        cls.quantity.domain = [
            cls.quantity.domain,
            If(Bool(Eval('asset_lot')),
                ('quantity', '=', 1),
                ()),
            ]
        cls.quantity.depends.append('asset_lot')

        cls._error_messages.update({
                'asset_overlapping_dates': (
                    'The lines "%(line1)s" and "%(line2)s" '
                    'for the same lot overlap.'),
                })

    @fields.depends('subscription', 'start_date', 'end_date',
        '_parent_subscription.start_date', '_parent_subscription.end_date')
    def on_change_subscription(self):
        if self.subscription:
            if not self.start_date:
                self.start_date = self.subscription.start_date
            if not self.end_date:
                self.end_date = self.subscription.end_date

    def _get_context_sale_price(self):
        context = {}
        if getattr(self, 'subscription', None):
            if getattr(self.subscription, 'currency', None):
                context['currency'] = self.subscription.currency.id
            if getattr(self.subscription, 'party', None):
                context['customer'] = self.subscription.party.id
            if getattr(self.subscription, 'start_date'):
                context['sale_date'] = self.subscription.start_date
        if self.unit:
            context['uom'] = self.unit.id
        elif self.service:
            context['uom'] = self.service.sale_uom.id
        # TODO tax
        return context

    def compute_next_consumption_date(self):
        if not self.consumption_recurrence:
            return None
        date = self.next_consumption_date or self.start_date
        rruleset = self.consumption_recurrence.rruleset(self.start_date)
        dt = datetime.datetime.combine(date, datetime.time())
        inc = (self.start_date == date) and not self.next_consumption_date
        next_date = rruleset.after(dt, inc=inc).date()
        for end_date in [self.end_date, self.subscription.end_date]:
            if end_date:
                if next_date > end_date:
                    return None
        return next_date

    @fields.depends('service')
    def on_change_with_asset_lot_required(self, name=None):
        if not self.service:
            return False
        return bool(self.service.asset_lots)

    @classmethod
    def copy(cls, lines, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('lot')
        return super(SubscriptionLine, cls).copy(lines, default)

    @classmethod
    def validate(cls, lines):
        super(SubscriptionLine, cls).validate(lines)
        cls._validate_dates(lines)

    @classmethod
    def _validate_dates(cls, lines):
        transaction = Transaction()
        connection = transaction.connection
        cursor = connection.cursor()

        transaction.database.lock(connection, cls._table)

        line = cls.__table__()
        other = cls.__table__()
        overlap_where = (
            ((line.end_date == Null)
                & ((other.end_date == Null)
                    | (other.start_date > line.start_date)
                    | (other.end_date > line.start_date)))
            | ((line.end_date != Null)
                & (((other.end_date == Null)
                        & (other.start_date < line.end_date))
                    | ((other.end_date != Null)
                        & (((other.end_date >= line.start_date)
                                & (other.end_date < line.end_date))
                            | ((other.start_date >= line.start_date)
                                & (other.start_date < line.end_date)))))))
        for sub_lines in grouped_slice(lines):
            sub_ids = [l.id for l in sub_lines]
            cursor.execute(*line.join(other,
                    condition=((line.id != other.id)
                        & (line.asset_lot == other.asset_lot))
                    ).select(line.id, other.id,
                    where=((line.asset_lot != Null)
                        & reduce_ids(line.id, sub_ids)
                        & overlap_where),
                    limit=1))
            overlapping = cursor.fetchone()
            if overlapping:
                sline1, sline2 = cls.browse(overlapping)
                cls.raise_user_error('asset_overlapping_dates', {
                        'line1': sline1.rec_name,
                        'line2': sline2.rec_name,
                        })
