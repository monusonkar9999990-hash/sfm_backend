"""The queries behind the dashboard and the reports.

This module owns no tables. Every figure is read from the modules that do, and
each function takes an already-scoped queryset so the caller decides *whose*
records are being counted and this file only decides *how*.

Two rules run through all of it:

**One query per block, not one per number.** Every count in a block is a
`filter=` argument on a single `aggregate()`, so the attendance block is one
round trip rather than five. The dashboard totals ten queries for six modules;
`DashboardQueryCountTests` holds that number to a ceiling.

**Say what the number actually is.** Several figures the brief asks for have no
exact column behind them. Where that happens the mapping is written down here
rather than guessed at by whoever reads the JSON:

* *skipped beats* — a `BeatPlan` has no skipped state. Individual stops do, so
  this counts skipped **stops**, and `missed_plans` is reported alongside it
  for the plan-level figure.
* *planned site visits* — a visit is created when somebody arrives, so nothing
  is ever in a "planned" state. This counts every visit **recorded** in the
  window. It is not a plan-versus-actual comparison, and reading it as one
  would overstate completion.
* *absent* — there is no holiday calendar in this system, so absence is
  measured against **calendar** days, not working days. `expected_records` and
  `days_in_range` are returned next to it so the basis is visible rather than
  implied.
"""

from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg, Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from attendance.models import Attendance
from beats.models import BeatPlan, BeatPlanStatus, BeatPlanVisit
from beats.models import VisitStatus as StopStatus
from customers.models import Customer
from orders.models import Order, OrderItem, OrderStatus
from products.models import LOW_STOCK_THRESHOLD, Product
from sitevisits.models import SiteVisit
from sitevisits.models import VisitStatus as SiteVisitStatus

ZERO = Value(Decimal('0.00'), output_field=DecimalField(max_digits=14, decimal_places=2))


def _money(value):
    """Aggregates come back as Decimal or None; the wire wants a number."""
    return float(value or 0)


def _aware(moment):
    return timezone.make_aware(moment) if settings.USE_TZ else moment


def day_bounds(day):
    """The half-open instant range `[start, next midnight)` for a local day.

    Used instead of `__date=` on every `DateTimeField` in this module, for two
    reasons — one of which is a bug this cost real time to find.

    **Correctness.** On MySQL, `__date` compiles to `CONVERT_TZ`, which
    returns NULL unless the server's timezone tables have been loaded with
    `mysql_tzinfo_to_sql`. On a server without them the filter matches nothing
    and the endpoint answers `0` — no error, no warning, just a dashboard
    quietly reporting that nobody visited anybody. Comparing against explicit
    bounds needs no timezone table.

    **Speed.** A function wrapped around a column cannot use an index on it.
    `check_in_at >= x AND check_in_at < y` is a range scan; `DATE(check_in_at)
    = x` is a full scan.
    """
    start = _aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def range_bounds(date_from, date_to):
    """The same, spanning `date_from` to the end of `date_to`."""
    start = _aware(datetime.combine(date_from, time.min))
    end = _aware(datetime.combine(date_to, time.min)) + timedelta(days=1)
    return start, end


def _hours(minutes):
    return round((minutes or 0) / 60, 2)


# --------------------------------------------------------------- attendance


def attendance_today(*, day, users_qs, attendance_qs):
    """Who was in today, out of the population `users_qs` describes."""
    total_employees = users_qs.count()

    counts = attendance_qs.filter(day=day).aggregate(
        checked_in=Count('id'),
        checked_out=Count('id', filter=Q(punch_out_at__isnull=False)),
        late=Count('id', filter=Q(is_late=True)),
    )

    checked_in = counts['checked_in']
    return {
        'total_employees': total_employees,
        'checked_in_today': checked_in,
        'checked_out_today': counts['checked_out'],
        # Never negative: an attendance row can outlive the user list it is
        # counted against — a suspended employee still punched in this morning.
        'absent_today': max(total_employees - checked_in, 0),
        'late_arrivals': counts['late'],
        'still_in': max(checked_in - counts['checked_out'], 0),
    }


def attendance_report(*, date_from, date_to, users_qs, attendance_qs):
    """Attendance across a range, with the basis for `absent` made explicit."""
    employees = users_qs.count()
    days_in_range = (date_to - date_from).days + 1
    expected = employees * days_in_range

    rows = attendance_qs.filter(day__gte=date_from, day__lte=date_to)
    # `_sum` suffix for the same reason as in `sales_report`: an alias named
    # after the column it sums shadows that column, and the Avg beside it then
    # resolves to the alias rather than the field.
    summary = rows.aggregate(
        present=Count('id'),
        late=Count('id', filter=Q(is_late=True)),
        closed=Count('id', filter=Q(punch_out_at__isnull=False)),
        worked_sum=Coalesce(Sum('worked_minutes'), 0),
        average_minutes=Avg('worked_minutes'),
    )

    return {
        'date_from': date_from,
        'date_to': date_to,
        'employees': employees,
        'days_in_range': days_in_range,
        # Calendar days, not working days — see the module docstring.
        'expected_records': expected,
        'present': summary['present'],
        'absent': max(expected - summary['present'], 0),
        'late': summary['late'],
        'completed_shifts': summary['closed'],
        'total_working_hours': _hours(summary['worked_sum']),
        'average_working_hours': _hours(summary['average_minutes']),
    }


# --------------------------------------------------------------------- beats


def _beat_counts(plans_qs, stops_qs):
    plans = plans_qs.aggregate(
        assigned=Count('id'),
        active=Count('id', filter=Q(status=BeatPlanStatus.IN_PROGRESS)),
        completed=Count('id', filter=Q(status=BeatPlanStatus.COMPLETED)),
        missed=Count('id', filter=Q(status=BeatPlanStatus.MISSED)),
        planned_outlets=Coalesce(Sum('planned_outlet_count'), 0),
    )
    stops = stops_qs.aggregate(
        covered=Count('id', filter=Q(status=StopStatus.VISITED)),
        skipped=Count('id', filter=Q(status=StopStatus.SKIPPED)),
        pending=Count('id', filter=Q(status=StopStatus.PENDING)),
    )
    return plans, stops


def beats_today(*, day, plans_qs):
    plans = plans_qs.filter(date=day)
    stops = BeatPlanVisit.objects.filter(plan__in=plans)
    plan_counts, stop_counts = _beat_counts(plans, stops)

    return {
        'assigned_beats': plan_counts['assigned'],
        'active_beats': plan_counts['active'],
        'completed_beats': plan_counts['completed'],
        # Stops, not beats — a plan has no skipped state. See the docstring.
        'skipped_stops': stop_counts['skipped'],
        'skipped_beats': stop_counts['skipped'],
        'missed_beats': plan_counts['missed'],
        'covered_stops': stop_counts['covered'],
        'pending_stops': stop_counts['pending'],
    }


def beat_report(*, date_from, date_to, plans_qs):
    plans = plans_qs.filter(date__gte=date_from, date__lte=date_to)
    stops = BeatPlanVisit.objects.filter(plan__in=plans)
    plan_counts, stop_counts = _beat_counts(plans, stops)

    assigned = plan_counts['assigned']
    started = plans.filter(started_at__isnull=False).count()
    completed = plan_counts['completed']

    planned_outlets = plan_counts['planned_outlets']
    covered = stop_counts['covered']

    return {
        'date_from': date_from,
        'date_to': date_to,
        'assigned': assigned,
        'started': started,
        'completed': completed,
        'skipped': stop_counts['skipped'],
        'missed': plan_counts['missed'],
        'completion_percentage': (
            round(completed / assigned * 100, 2) if assigned else None
        ),
        'planned_outlets': planned_outlets,
        'covered_outlets': covered,
        'coverage_percentage': (
            round(covered / planned_outlets * 100, 2) if planned_outlets else None
        ),
    }


# --------------------------------------------------------------- site visits


def site_visits_today(*, day, visits_qs):
    start, end = day_bounds(day)
    counts = visits_qs.filter(check_in_at__gte=start, check_in_at__lt=end).aggregate(
        recorded=Count('id'),
        completed=Count('id', filter=Q(status=SiteVisitStatus.COMPLETED)),
        cancelled=Count('id', filter=Q(status=SiteVisitStatus.CANCELLED)),
        open_now=Count('id', filter=Q(status=SiteVisitStatus.IN_PROGRESS)),
    )

    return {
        # There is no planned state on a visit — see the module docstring.
        'planned_visits': counts['recorded'],
        'recorded_visits': counts['recorded'],
        'completed_visits': counts['completed'],
        'cancelled_visits': counts['cancelled'],
        'open_visits': counts['open_now'],
    }


def site_visit_report(*, date_from, date_to, visits_qs):
    start, end = range_bounds(date_from, date_to)
    rows = visits_qs.filter(check_in_at__gte=start, check_in_at__lt=end)
    counts = rows.aggregate(
        recorded=Count('id'),
        completed=Count('id', filter=Q(status=SiteVisitStatus.COMPLETED)),
        cancelled=Count('id', filter=Q(status=SiteVisitStatus.CANCELLED)),
        open_now=Count('id', filter=Q(status=SiteVisitStatus.IN_PROGRESS)),
        average_minutes=Avg(
            'duration_minutes', filter=Q(status=SiteVisitStatus.COMPLETED)
        ),
    )

    return {
        'date_from': date_from,
        'date_to': date_to,
        'planned': counts['recorded'],
        'recorded': counts['recorded'],
        'completed': counts['completed'],
        'cancelled': counts['cancelled'],
        'open': counts['open_now'],
        'average_visit_minutes': (
            round(counts['average_minutes'], 2)
            if counts['average_minutes'] is not None
            else None
        ),
    }


# ----------------------------------------------------------------- customers


def customer_counts(*, day=None, date_from=None, date_to=None, customers_qs=None):
    """Customer totals. `day` gives today's intake; a range gives the window's."""
    queryset = customers_qs if customers_qs is not None else Customer.objects.all()

    new_filter = Q()
    if day is not None:
        start, end = day_bounds(day)
        new_filter = Q(created_at__gte=start, created_at__lt=end)
    elif date_from is not None and date_to is not None:
        start, end = range_bounds(date_from, date_to)
        new_filter = Q(created_at__gte=start, created_at__lt=end)

    return queryset.aggregate(
        total_customers=Count('id'),
        active_customers=Count('id', filter=Q(is_active=True)),
        inactive_customers=Count('id', filter=Q(is_active=False)),
        new_customers=Count('id', filter=new_filter) if new_filter else Count('id'),
    )


# ------------------------------------------------------------------ products


def product_counts(*, products_qs=None):
    """Catalogue health. Not date-filtered: a product's stock level is a fact
    about now, not about a window."""
    queryset = products_qs if products_qs is not None else Product.objects.all()

    return queryset.aggregate(
        total_products=Count('id'),
        active_products=Count('id', filter=Q(active=True)),
        inactive_products=Count('id', filter=Q(active=False)),
        low_stock_products=Count(
            'id',
            filter=Q(
                active=True,
                stock_quantity__gt=0,
                stock_quantity__lte=LOW_STOCK_THRESHOLD,
            ),
        ),
        out_of_stock_products=Count(
            'id', filter=Q(active=True, stock_quantity=0)
        ),
    )


# -------------------------------------------------------------------- orders

# Cancelled orders are excluded from every money figure. They are counted, so
# a cancellation rate is visible, but an order that was called off is not
# revenue and must never reach a sales total.
BOOKED = ~Q(status=OrderStatus.CANCELLED)


def orders_today(*, day, orders_qs):
    today = orders_qs.filter(order_date=day).aggregate(
        total_orders=Count('id'),
        submitted=Count('id', filter=Q(status=OrderStatus.SUBMITTED)),
        cancelled=Count('id', filter=Q(status=OrderStatus.CANCELLED)),
        draft=Count('id', filter=Q(status=OrderStatus.DRAFT)),
        sales=Coalesce(Sum('grand_total', filter=BOOKED), ZERO),
    )

    month_start = day.replace(day=1)
    monthly = orders_qs.filter(
        order_date__gte=month_start, order_date__lte=day
    ).aggregate(
        orders=Count('id'),
        sales=Coalesce(Sum('grand_total', filter=BOOKED), ZERO),
    )

    return {
        'total_orders_today': today['total_orders'],
        'submitted_orders': today['submitted'],
        'cancelled_orders': today['cancelled'],
        'draft_orders': today['draft'],
        'todays_sales': _money(today['sales']),
        'monthly_orders': monthly['orders'],
        'monthly_sales': _money(monthly['sales']),
        'month_start': month_start,
    }


def sales_report(*, date_from, date_to, orders_qs, limit=5):
    """Money booked in a window, and who and what it came from."""
    rows = orders_qs.filter(order_date__gte=date_from, order_date__lte=date_to)

    # The aliases carry a `_sum` suffix on purpose. Naming one of them
    # `grand_total` shadows the column, and the `Avg('grand_total')` beside it
    # then resolves to the alias instead of the field — Django refuses with
    # "Cannot compute Avg('grand_total'): 'grand_total' is an aggregate".
    totals = rows.aggregate(
        order_count=Count('id'),
        booked_count=Count('id', filter=BOOKED),
        cancelled_count=Count('id', filter=Q(status=OrderStatus.CANCELLED)),
        subtotal_sum=Coalesce(Sum('subtotal', filter=BOOKED), ZERO),
        discount_sum=Coalesce(Sum('discount_total', filter=BOOKED), ZERO),
        gst_sum=Coalesce(Sum('gst_total', filter=BOOKED), ZERO),
        grand_sum=Coalesce(Sum('grand_total', filter=BOOKED), ZERO),
        average_order_value=Avg('grand_total', filter=BOOKED),
    )

    # One grouped query each, not one per customer.
    top_customers = list(
        rows.filter(BOOKED)
        .values('customer_id', name=F('customer__name'), code=F('customer__code'))
        .annotate(order_count=Count('id'), total=Coalesce(Sum('grand_total'), ZERO))
        .order_by('-total')[:limit]
    )

    top_products = list(
        OrderItem.objects.filter(order__in=rows.filter(BOOKED))
        .values(
            'product_id',
            name=F('product__name'),
            code=F('product__product_code'),
        )
        .annotate(
            quantity=Coalesce(Sum('quantity'), 0),
            total=Coalesce(Sum('line_total'), ZERO),
        )
        .order_by('-total')[:limit]
    )

    return {
        'date_from': date_from,
        'date_to': date_to,
        'order_count': totals['order_count'],
        'booked_count': totals['booked_count'],
        'cancelled_count': totals['cancelled_count'],
        'subtotal': _money(totals['subtotal_sum']),
        'discount_total': _money(totals['discount_sum']),
        'gst_total': _money(totals['gst_sum']),
        'grand_total': _money(totals['grand_sum']),
        # None, not 0, when nothing was booked: the average of no orders does
        # not exist, and 0 would read as "we sold nothing at all".
        'average_order_value': (
            round(float(totals['average_order_value']), 2)
            if totals['average_order_value'] is not None
            else None
        ),
        'top_customers': [
            {
                'customer_id': str(row['customer_id']),
                'name': row['name'],
                'code': row['code'],
                'order_count': row['order_count'],
                'total': _money(row['total']),
            }
            for row in top_customers
        ],
        'top_products': [
            {
                'product_id': str(row['product_id']),
                'name': row['name'],
                'code': row['code'],
                'quantity': row['quantity'],
                'total': _money(row['total']),
            }
            for row in top_products
        ],
    }


# ------------------------------------------------------------------- scoping


def is_manager(user):
    """True when this user's figures may cover other people."""
    return bool(user and user.has_perm('accounts.view_team_reports'))


def scoped_querysets(user):
    """The six querysets every figure is drawn from, narrowed to what this
    user may see.

    A manager's dashboard covers the organisation; everyone else's covers
    themselves. The shape of the response is identical either way — only the
    population changes — and `scope` in the payload says which it was, so a
    client is never left guessing whether a small number means a quiet day or
    a narrow view.
    """
    from accounts.models import User

    if is_manager(user):
        return {
            'users': User.objects.filter(status=User.Status.ACTIVE),
            'attendance': Attendance.objects.all(),
            'plans': BeatPlan.objects.all(),
            'visits': SiteVisit.objects.all(),
            'orders': Order.objects.all(),
            'customers': Customer.objects.all(),
            'products': Product.objects.all(),
        }

    return {
        'users': User.objects.filter(pk=user.pk),
        'attendance': Attendance.objects.filter(user=user),
        'plans': BeatPlan.objects.filter(user=user),
        'visits': SiteVisit.objects.filter(user=user),
        'orders': Order.objects.filter(employee=user),
        # Customers and products are master data — everybody sells from the
        # same catalogue to the same book of customers, so these are not
        # narrowed by who is asking.
        'customers': Customer.objects.all(),
        'products': Product.objects.all(),
    }


# -------------------------------------------------------------------- trends


def _empty_day(day):
    return {
        'date': day,
        'present': 0,
        'late': 0,
        'absent': 0,
        'new_customers': 0,
        'site_visits': 0,
        'visits_completed': 0,
        'beats_assigned': 0,
        'beats_completed': 0,
        'orders': 0,
        'sales': Decimal('0.00'),
    }


def daily_trends(*, date_from, date_to, users_qs, attendance_qs, customers_qs,
                 visits_qs, plans_qs, orders_qs):
    """One row per calendar day across the range, for the charts.

    Every existing report answers "how many in this window". A chart needs
    "how many on each day", and nothing here produced that — so the trend
    screens had no data source at all and would have had to invent one.

    Six grouped queries, not six-times-N: each block groups by day in the
    database and the results are merged onto a pre-built calendar. Querying
    per day would be ninety round trips for a quarter.

    Days with no activity are present with zeros. A chart that silently skips
    empty days draws a line implying work happened continuously, which is the
    opposite of what a gap means.
    """
    days = (date_to - date_from).days + 1
    calendar = {
        date_from + timedelta(days=offset): _empty_day(date_from + timedelta(days=offset))
        for offset in range(days)
    }

    employees = users_qs.count()

    # Attendance is stored with a `day` column already, so it groups directly.
    attendance = (
        attendance_qs.filter(day__gte=date_from, day__lte=date_to)
        .values('day')
        .annotate(
            present=Count('id'),
            late=Count('id', filter=Q(is_late=True)),
        )
    )
    for row in attendance:
        entry = calendar.get(row['day'])
        if entry is None:
            continue
        entry['present'] = row['present']
        entry['late'] = row['late']
        # Calendar days, not working days — the same basis the window report
        # uses, and stated there for the same reason.
        entry['absent'] = max(employees - row['present'], 0)

    for day, entry in calendar.items():
        if entry['present'] == 0:
            entry['absent'] = employees

    start, end = range_bounds(date_from, date_to)

    # The rest are DateTimeFields with no date column beside them, so the day
    # has to be worked out rather than grouped on.
    #
    # NOT in the database. `TruncDate` compiles to `CONVERT_TZ` on MySQL and
    # returns NULL unless the server's timezone tables have been loaded with
    # `mysql_tzinfo_to_sql` — the same trap `day_bounds` documents for
    # `__date`. On this project's own server it grouped all 432 customers
    # under NULL and the endpoint answered zero for every day: no error, no
    # warning, just a chart showing that nobody had done anything.
    #
    # So the range is bounded in SQL (an index range scan) and only the
    # timestamps come back, bucketed here in the project's timezone. One query
    # each, and correct on a MySQL without timezone tables.
    for created_at in customers_qs.filter(
        created_at__gte=start, created_at__lt=end
    ).values_list('created_at', flat=True):
        entry = calendar.get(timezone.localtime(created_at).date())
        if entry is not None:
            entry['new_customers'] += 1

    for check_in_at, status in visits_qs.filter(
        check_in_at__gte=start, check_in_at__lt=end
    ).values_list('check_in_at', 'status'):
        entry = calendar.get(timezone.localtime(check_in_at).date())
        if entry is None:
            continue
        entry['site_visits'] += 1
        if status == SiteVisitStatus.COMPLETED:
            entry['visits_completed'] += 1

    # Beat plans carry a `date` column, so no truncation is needed.
    plans = (
        plans_qs.filter(date__gte=date_from, date__lte=date_to)
        .values('date')
        .annotate(
            assigned=Count('id'),
            completed=Count('id', filter=Q(status=BeatPlanStatus.COMPLETED)),
        )
    )
    for row in plans:
        entry = calendar.get(row['date'])
        if entry is not None:
            entry['beats_assigned'] = row['assigned']
            entry['beats_completed'] = row['completed']

    # Cancelled orders are counted but excluded from money, exactly as
    # `sales_report` does — an order that was called off is not revenue.
    orders = (
        orders_qs.filter(order_date__gte=date_from, order_date__lte=date_to)
        .values('order_date')
        .annotate(
            total=Count('id'),
            value=Coalesce(Sum('grand_total', filter=BOOKED), ZERO),
        )
    )
    for row in orders:
        entry = calendar.get(row['order_date'])
        if entry is not None:
            entry['orders'] = row['total']
            entry['sales'] = _money(row['value'])

    return {
        'date_from': date_from,
        'date_to': date_to,
        'employees': employees,
        'days': [calendar[day] for day in sorted(calendar)],
    }


# -------------------------------------------------------------- team rollup


def team_directory(*, users_qs):
    """The people inside the caller's scope, for a filter control.

    The administration module already lists employees, but reading it needs
    `manage_users` — a staff list is personal data there, with mobile numbers,
    joining dates and status on it. A supervisor who may read the team's
    figures but may not administer accounts still has to be able to pick a name
    out of a drop-down, and this answers exactly that: identity and posting,
    nothing personal, scoped the same way every other figure on this endpoint
    is.
    """
    return [
        {
            'employee_id': str(user.id),
            'employee_code': user.employee_code,
            'employee_name': user.full_name,
            'role': user.role.name if user.role_id else '',
            'territory': (
                user.primary_territory.name if user.primary_territory else ''
            ),
        }
        for user in users_qs.select_related('role')
        .prefetch_related('territory_links__territory')
        .order_by('full_name')
    ]


def team_rollup(*, date_from, date_to, users_qs, attendance_qs, customers_qs,
                visits_qs, plans_qs, orders_qs):
    """One row per person for the whole window — the league table.

    `report_rows` answers "who did what on which day", which is the audit
    trail. Management asks a different question first: across this month, how
    does each person compare. Deriving that in a client from the daily table
    would mean shipping every row to add them up, and would silently be wrong
    the moment the table hit its `limit`.

    Everybody in scope gets a row, including people with no activity at all —
    a rep who booked nothing is exactly who a comparison exists to surface, and
    dropping them makes the quiet weeks invisible.

    Five grouped queries regardless of headcount or range length.
    """
    people = list(
        users_qs.select_related('role')
        .prefetch_related('territory_links__territory')
        .order_by('full_name')
    )
    user_ids = [user.id for user in people]

    rows = {
        user.id: {
            'employee_id': str(user.id),
            'employee_code': user.employee_code,
            'employee_name': user.full_name,
            'role': user.role.name if user.role_id else '',
            'territory': (
                user.primary_territory.name if user.primary_territory else ''
            ),
            'days_present': 0,
            'days_late': 0,
            'days_completed': 0,
            'total_working_hours': 0.0,
            'average_working_hours': 0.0,
            'new_customers': 0,
            'site_visits': 0,
            'site_visits_completed': 0,
            'beats_assigned': 0,
            'beats_completed': 0,
            'beat_completion_percentage': None,
            'orders': 0,
            'cancelled_orders': 0,
            'sales': 0.0,
            'average_order_value': None,
        }
        for user in people
    }

    if not user_ids:
        return {
            'date_from': date_from,
            'date_to': date_to,
            'days_in_range': (date_to - date_from).days + 1,
            'employees': 0,
            'rows': [],
        }

    start, end = range_bounds(date_from, date_to)

    for row in (
        attendance_qs.filter(
            day__gte=date_from, day__lte=date_to, user_id__in=user_ids
        )
        .values('user_id')
        .annotate(
            present=Count('id'),
            late=Count('id', filter=Q(is_late=True)),
            completed=Count('id', filter=Q(punch_out_at__isnull=False)),
            minutes=Coalesce(Sum('worked_minutes'), Value(0)),
        )
    ):
        entry = rows[row['user_id']]
        entry['days_present'] = row['present']
        entry['days_late'] = row['late']
        entry['days_completed'] = row['completed']
        entry['total_working_hours'] = _hours(row['minutes'])
        # Averaged over days worked, not days in the range: a person who was
        # on leave for half of it did not work half-days.
        entry['average_working_hours'] = (
            round(_hours(row['minutes']) / row['present'], 2)
            if row['present']
            else 0.0
        )

    for row in (
        visits_qs.filter(
            check_in_at__gte=start, check_in_at__lt=end, user_id__in=user_ids
        )
        .values('user_id')
        .annotate(
            total=Count('id'),
            done=Count('id', filter=Q(status=SiteVisitStatus.COMPLETED)),
        )
    ):
        entry = rows[row['user_id']]
        entry['site_visits'] = row['total']
        entry['site_visits_completed'] = row['done']

    for row in (
        plans_qs.filter(
            date__gte=date_from, date__lte=date_to, user_id__in=user_ids
        )
        .values('user_id')
        .annotate(
            assigned=Count('id'),
            completed=Count('id', filter=Q(status=BeatPlanStatus.COMPLETED)),
        )
    ):
        entry = rows[row['user_id']]
        entry['beats_assigned'] = row['assigned']
        entry['beats_completed'] = row['completed']
        entry['beat_completion_percentage'] = (
            round(row['completed'] * 100 / row['assigned'], 1)
            if row['assigned']
            else None
        )

    for row in (
        customers_qs.filter(
            created_at__gte=start, created_at__lt=end, created_by_id__in=user_ids
        )
        .values('created_by_id')
        .annotate(total=Count('id'))
    ):
        rows[row['created_by_id']]['new_customers'] = row['total']

    for row in (
        orders_qs.filter(
            order_date__gte=date_from,
            order_date__lte=date_to,
            employee_id__in=user_ids,
        )
        .values('employee_id')
        .annotate(
            total=Count('id', filter=BOOKED),
            cancelled=Count('id', filter=Q(status=OrderStatus.CANCELLED)),
            value=Coalesce(Sum('grand_total', filter=BOOKED), ZERO),
        )
    ):
        entry = rows[row['employee_id']]
        entry['orders'] = row['total']
        entry['cancelled_orders'] = row['cancelled']
        entry['sales'] = _money(row['value'])
        entry['average_order_value'] = (
            round(_money(row['value']) / row['total'], 2) if row['total'] else None
        )

    # Ranked by what management ranks by. A stable tie-break on name keeps two
    # people with no orders from swapping places between refreshes.
    ordered = sorted(
        rows.values(),
        key=lambda entry: (-entry['sales'], -entry['orders'], entry['employee_name']),
    )

    return {
        'date_from': date_from,
        'date_to': date_to,
        'days_in_range': (date_to - date_from).days + 1,
        'employees': len(ordered),
        'rows': ordered,
    }


# -------------------------------------------------------------- visit log


def visit_log(*, date_from, date_to, visits_qs, status=None, limit=500):
    """Every site visit in the window, one row each.

    `/site-visits/` answers with the caller's *own* visits and nothing else —
    correct for the phone, where a rep is looking at their own day, and useless
    to a manager who needs to see where the team actually went. Rather than
    widening that endpoint and quietly changing what the mobile app receives,
    this is the management view: the same records, scoped the way every other
    figure in this module is scoped.

    Flat on purpose. A table wants a row, not a nested site object, and the
    photos are counted rather than listed — a manager scanning fifty visits
    wants to know which ones have evidence, not to download it.
    """
    start, end = range_bounds(date_from, date_to)

    visits = visits_qs.filter(check_in_at__gte=start, check_in_at__lt=end)
    if status:
        visits = visits.filter(status=status)

    total = visits.count()
    visits = (
        visits.select_related('user', 'site')
        .annotate(photo_count=Count('images'))
        .order_by('-check_in_at')[:limit]
    )

    rows = [
        {
            'visit_id': str(visit.id),
            'date': timezone.localtime(visit.check_in_at).date(),
            'employee_id': str(visit.user_id),
            'employee_code': visit.user.employee_code,
            'employee_name': visit.user.full_name,
            'site_name': visit.site.name,
            'customer_name': visit.site.customer_name,
            'purpose': visit.purpose,
            'status': visit.status,
            'check_in_at': visit.check_in_at,
            'check_out_at': visit.check_out_at,
            'duration_minutes': visit.duration_minutes,
            'check_in_distance_meters': visit.check_in_distance_meters,
            'stage_observed': visit.stage_observed,
            'expected_order_value': _money(visit.expected_order_value),
            'follow_up_date': visit.follow_up_date,
            'photo_count': visit.photo_count,
            'remarks': visit.remarks,
        }
        for visit in visits
    ]

    return {
        'date_from': date_from,
        'date_to': date_to,
        'total_rows': total,
        'rows': rows,
    }


# ------------------------------------------------------------ tabular report


def report_rows(*, date_from, date_to, users_qs, attendance_qs, customers_qs,
                visits_qs, plans_qs, orders_qs, limit=500):
    """One row per person per day, for the table view.

    The window reports answer "how many across everybody"; a table has to say
    *who*, on *which day*. That is a different shape and nothing here produced
    it, so the table screen had nothing to read.

    Built from attendance rows outward: a day somebody worked is a row, and
    the other modules' counts for that person-day are merged onto it. Somebody
    who never punched in has no row rather than a row of zeros — an absence is
    already visible in the attendance report, and a table of mostly-empty rows
    for every employee times every day is unreadable at any real headcount.

    Five grouped queries regardless of range length. `limit` caps the rows
    returned, because a quarter across forty people is 3,600 rows and no
    phone screen wants them; the count before truncation is reported so the
    caller can say "showing 500 of 3,600" rather than silently misleading.
    """
    attendance = list(
        attendance_qs.filter(day__gte=date_from, day__lte=date_to)
        .select_related('user')
        .order_by('-day', 'user__employee_code')
    )

    total = len(attendance)
    attendance = attendance[:limit]

    # The person-days actually on the page, so the merges below fetch only
    # what is about to be displayed.
    wanted = {(row.user_id, row.day) for row in attendance}
    if not wanted:
        return {
            'date_from': date_from,
            'date_to': date_to,
            'total_rows': total,
            'rows': [],
        }

    user_ids = {user_id for user_id, _ in wanted}
    start, end = range_bounds(date_from, date_to)

    # Bucketed here rather than with TruncDate, for the reason spelled out in
    # `daily_trends`: on MySQL without timezone tables it groups everything
    # under NULL and reports zero.
    visits = {}
    for user_id, check_in_at, status in visits_qs.filter(
        check_in_at__gte=start, check_in_at__lt=end, user_id__in=user_ids
    ).values_list('user_id', 'check_in_at', 'status'):
        key = (user_id, timezone.localtime(check_in_at).date())
        row = visits.setdefault(key, {'total': 0, 'done': 0})
        row['total'] += 1
        if status == SiteVisitStatus.COMPLETED:
            row['done'] += 1

    orders = {
        (row['employee_id'], row['order_date']): row
        for row in orders_qs.filter(
            order_date__gte=date_from,
            order_date__lte=date_to,
            employee_id__in=user_ids,
        )
        .values('employee_id', 'order_date')
        .annotate(
            total=Count('id'),
            value=Coalesce(Sum('grand_total', filter=BOOKED), ZERO),
        )
    }

    # Customers carry `created_by`, which is the person who onboarded them.
    customers = {}
    for created_by_id, created_at in customers_qs.filter(
        created_at__gte=start, created_at__lt=end, created_by_id__in=user_ids
    ).values_list('created_by_id', 'created_at'):
        key = (created_by_id, timezone.localtime(created_at).date())
        customers[key] = {'total': customers.get(key, {}).get('total', 0) + 1}

    plans = {
        (row['user_id'], row['date']): row
        for row in plans_qs.filter(
            date__gte=date_from, date__lte=date_to, user_id__in=user_ids
        )
        .values('user_id', 'date')
        .annotate(
            assigned=Count('id'),
            completed=Count('id', filter=Q(status=BeatPlanStatus.COMPLETED)),
        )
    }

    rows = []
    for record in attendance:
        key = (record.user_id, record.day)
        visit = visits.get(key, {})
        order = orders.get(key, {})
        plan = plans.get(key, {})
        assigned = plan.get('assigned', 0)
        completed = plan.get('completed', 0)

        rows.append(
            {
                'date': record.day,
                'employee_id': str(record.user_id),
                'employee_code': record.user.employee_code,
                'employee_name': record.user.full_name,
                # Derived, not stored: `Attendance` has no status column. A row
                # existing means the person punched in, and `is_late` is the
                # only distinction the model carries. Absent days have no row
                # at all, which is why they are not in this table.
                'attendance_status': 'late' if record.is_late else 'present',
                'check_in_at': record.punch_in_at,
                'check_out_at': record.punch_out_at,
                'is_late': record.is_late,
                'worked_hours': _hours(record.worked_minutes),
                # Null rather than 0 where no geofence was assigned: zero
                # metres means "standing on it", which is a different claim.
                'check_in_distance_meters': record.punch_in_distance_meters,
                'within_geofence': record.punch_in_within_fence,
                'new_customers': customers.get(key, {}).get('total', 0),
                'site_visits': visit.get('total', 0),
                'site_visits_completed': visit.get('done', 0),
                'beats_assigned': assigned,
                'beats_completed': completed,
                'beat_completion_percentage': (
                    round(completed * 100 / assigned, 1) if assigned else None
                ),
                'orders': order.get('total', 0),
                'sales': _money(order.get('value') or Decimal('0.00')),
            }
        )

    return {
        'date_from': date_from,
        'date_to': date_to,
        'total_rows': total,
        'rows': rows,
    }
