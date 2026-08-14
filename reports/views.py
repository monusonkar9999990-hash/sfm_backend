"""Dashboard and report endpoints.

Read-only, every one of them: no view here writes a row, and none takes a
method other than GET. Each view validates its query string with a serializer,
hands already-scoped querysets to `services`, and renders what comes back.
"""

import csv

from django.http import HttpResponse
from django.utils import timezone
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import services
from .permissions import CanViewReports
from .serializers import (
    AttendanceReportFilterSerializer,
    AttendanceReportSerializer,
    BeatReportSerializer,
    CustomerReportSerializer,
    DashboardSerializer,
    ProductReportSerializer,
    ReportFilterSerializer,
    ReportTableFilterSerializer,
    ReportTableSerializer,
    SalesReportFilterSerializer,
    SalesReportSerializer,
    SiteVisitReportSerializer,
    TeamDirectorySerializer,
    TeamReportSerializer,
    TrendsSerializer,
    VisitLogFilterSerializer,
    VisitLogSerializer,
)


class ReportBaseView(GenericAPIView):
    """Shared plumbing: authentication, the permission, and the scoping."""

    permission_classes = [IsAuthenticated, CanViewReports]
    filter_serializer_class = ReportFilterSerializer

    # Read-only, and DRF's OPTIONS should say so.
    http_method_names = ['get', 'head', 'options']

    def filters(self):
        serializer = self.filter_serializer_class(data=self.request.query_params)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def scoped(self):
        return services.scoped_querysets(self.request.user)

    @property
    def scope_label(self):
        return 'team' if services.is_manager(self.request.user) else 'self'

    # ------------------------------------------------------------------ csv

    @property
    def wants_csv(self):
        """`?export=csv`, spelled that way on purpose.

        Not `?format=csv`: `format` is DRF's own content-negotiation parameter,
        and asking for a renderer that is not installed answers 404 rather than
        the file somebody clicked a button for.
        """
        return self.request.query_params.get('export', '').lower() == 'csv'

    def csv_response(self, *, filename, rows, columns):
        """A spreadsheet of `rows`, one line per row.

        `columns` is an ordered list of `(key, header)` — explicit rather than
        taken from the first row's keys, so a column cannot appear or vanish
        depending on which record happened to be first, and the header row
        reads like something a person wrote.
        """
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'

        # Excel reads a bare UTF-8 CSV as the system codepage and mangles any
        # name outside ASCII; the BOM is what tells it otherwise.
        response.write('﻿')

        writer = csv.writer(response)
        writer.writerow([header for _, header in columns])
        for row in rows:
            writer.writerow(
                ['' if row.get(key) is None else row.get(key) for key, _ in columns]
            )
        return response


class DashboardView(ReportBaseView):
    """Today, across every module, in one response.

    Ten queries for six modules — every count inside a block is a `filter=`
    on one `aggregate()` rather than a query of its own.

    A manager's figures cover the organisation; everyone else's cover
    themselves. The response shape is identical either way, and `scope` says
    which one you got.

    **Responses**
    * `200` — the dashboard
    * `401` — missing or invalid access token
    * `403` — the role does not include reports
    """

    serializer_class = DashboardSerializer

    def get(self, request, *args, **kwargs):
        day = timezone.localdate()
        qs = self.scoped()

        payload = {
            'scope': self.scope_label,
            'date': day,
            'generated_at': timezone.now(),
            'attendance': services.attendance_today(
                day=day, users_qs=qs['users'], attendance_qs=qs['attendance']
            ),
            'beats': services.beats_today(day=day, plans_qs=qs['plans']),
            'site_visits': services.site_visits_today(
                day=day, visits_qs=qs['visits']
            ),
            'customers': services.customer_counts(
                day=day, customers_qs=qs['customers']
            ),
            'products': services.product_counts(products_qs=qs['products']),
            'orders': services.orders_today(day=day, orders_qs=qs['orders']),
        }

        return Response(self.get_serializer(payload).data)


class SalesReportView(ReportBaseView):
    """Money booked in a window, and who and what it came from.

    **Query** `date_from`, `date_to` (default: the last 30 days),
    `employee_id`, `customer_id`, `limit` (top-N size, 1–50, default 5)

    Cancelled orders are counted but excluded from every money figure — an
    order that was called off is not revenue.

    `employee_id` is only meaningful to a caller whose scope covers other
    people; for anyone else the queryset is already their own.

    **Responses**
    * `200` — the report
    * `400` — a malformed or backwards date range
    * `401` — missing or invalid access token
    * `403` — the role does not include reports
    """

    serializer_class = SalesReportSerializer
    filter_serializer_class = SalesReportFilterSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()
        orders = self.scoped()['orders']

        if filters.get('employee_id'):
            orders = orders.filter(employee_id=filters['employee_id'])
        if filters.get('customer_id'):
            orders = orders.filter(customer_id=filters['customer_id'])

        payload = services.sales_report(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            orders_qs=orders,
            limit=filters.get('limit') or 5,
        )
        return Response(self.get_serializer(payload).data)


class AttendanceReportView(ReportBaseView):
    """Attendance across a window.

    **Query** `date_from`, `date_to`, `employee` (or `employee_id`)

    `absent` is measured against calendar days — this system has no holiday
    calendar — so `days_in_range` and `expected_records` are returned beside
    it rather than leaving the basis implied.

    **Responses**
    * `200` — the report
    * `400` — a malformed or backwards date range
    * `401` / `403` — as above
    """

    serializer_class = AttendanceReportSerializer
    filter_serializer_class = AttendanceReportFilterSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()
        qs = self.scoped()
        users, attendance = qs['users'], qs['attendance']

        if filters.get('employee'):
            users = users.filter(pk=filters['employee'])
            attendance = attendance.filter(user_id=filters['employee'])

        payload = services.attendance_report(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            users_qs=users,
            attendance_qs=attendance,
        )
        return Response(self.get_serializer(payload).data)


class BeatReportView(ReportBaseView):
    """Beat coverage across a window.

    `skipped` counts skipped **stops**; a plan has no skipped state, so
    `missed` is the plan-level figure beside it.

    **Query** `date_from`, `date_to`

    **Responses** `200` · `400` · `401` · `403`
    """

    serializer_class = BeatReportSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()

        payload = services.beat_report(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            plans_qs=self.scoped()['plans'],
        )
        return Response(self.get_serializer(payload).data)


class SiteVisitReportView(ReportBaseView):
    """Site visits across a window.

    `planned` counts visits **recorded** — a visit is created on arrival, so
    nothing is ever in a planned state. It is not a plan-versus-actual figure.

    **Query** `date_from`, `date_to`

    **Responses** `200` · `400` · `401` · `403`
    """

    serializer_class = SiteVisitReportSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()

        payload = services.site_visit_report(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            visits_qs=self.scoped()['visits'],
        )
        return Response(self.get_serializer(payload).data)


class CustomerReportView(ReportBaseView):
    """The customer book, and what the window added to it.

    **Query** `date_from`, `date_to` — these bound `new_customers` only; the
    totals are counts of the book as it stands.

    **Responses** `200` · `400` · `401` · `403`
    """

    serializer_class = CustomerReportSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()

        counts = services.customer_counts(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            customers_qs=self.scoped()['customers'],
        )
        payload = {
            'date_from': filters['date_from'],
            'date_to': filters['date_to'],
            **counts,
        }
        return Response(self.get_serializer(payload).data)


class ProductReportView(ReportBaseView):
    """Catalogue health.

    Takes no date range: a stock level is a fact about now, not about a
    window, and a `?date_from=` that quietly did nothing would be worse than
    not offering one.

    **Responses** `200` · `401` · `403`
    """

    serializer_class = ProductReportSerializer

    def get(self, request, *args, **kwargs):
        payload = {
            **services.product_counts(products_qs=self.scoped()['products']),
            'low_stock_threshold': services.LOW_STOCK_THRESHOLD,
        }
        return Response(self.get_serializer(payload).data)


class TrendsView(ReportBaseView):
    """Per-day figures across a window, for charts.

    Every other report answers "how many in this window". A chart needs one
    point per day, which nothing here produced — a trend screen built on the
    window reports could only have invented the shape between two totals.

    Days with no activity come back with zeros rather than being omitted. A
    line drawn through missing days implies work continued through them, which
    is the opposite of what a gap means.

    **Query** `date_from`, `date_to` (default: the last 30 days), `employee`

    **Responses**
    * `200` — one row per calendar day
    * `400` — a malformed or backwards date range
    * `401` — missing or invalid access token
    * `403` — the role does not include reports
    """

    serializer_class = TrendsSerializer
    filter_serializer_class = ReportTableFilterSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()
        qs = self.scoped()

        users, attendance = qs['users'], qs['attendance']
        visits, plans, orders = qs['visits'], qs['plans'], qs['orders']

        # Narrowing to one person is only meaningful to a caller whose scope
        # covers more than themselves; for anyone else it is already theirs.
        if filters.get('employee'):
            users = users.filter(pk=filters['employee'])
            attendance = attendance.filter(user_id=filters['employee'])
            visits = visits.filter(user_id=filters['employee'])
            plans = plans.filter(user_id=filters['employee'])
            orders = orders.filter(employee_id=filters['employee'])

        payload = services.daily_trends(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            users_qs=users,
            attendance_qs=attendance,
            customers_qs=qs['customers'],
            visits_qs=visits,
            plans_qs=plans,
            orders_qs=orders,
        )

        if self.wants_csv:
            return self.csv_response(
                filename=(
                    f'trends-{payload["date_from"]}-to-{payload["date_to"]}.csv'
                ),
                rows=payload['days'],
                columns=[
                    ('date', 'Date'),
                    ('present', 'Present'),
                    ('late', 'Late'),
                    ('absent', 'Absent'),
                    ('new_customers', 'New customers'),
                    ('site_visits', 'Site visits'),
                    ('visits_completed', 'Visits completed'),
                    ('beats_assigned', 'Beats assigned'),
                    ('beats_completed', 'Beats completed'),
                    ('orders', 'Orders'),
                    ('sales', 'Sales'),
                ],
            )

        return Response(self.get_serializer(payload).data)


class TeamDirectoryView(ReportBaseView):
    """The people this caller's figures cover, for a filter control.

    `/admin/employees/` already lists staff, but reading it needs
    `manage_users` — that list carries mobile numbers, joining dates and
    account status, which is personal data rather than a directory. A
    supervisor who may read the team's figures but may not administer accounts
    still has to pick a name out of a drop-down, so this answers with identity
    and posting only, scoped exactly as every other figure here is.

    A caller without `view_team_reports` gets one row: themselves.

    **Responses**
    * `200` — `{scope, count, employees}`
    * `401` — missing or invalid access token
    * `403` — the role does not include reports
    """

    serializer_class = TeamDirectorySerializer

    def get(self, request, *args, **kwargs):
        payload = {
            'scope': self.scope_label,
            'employees': services.team_directory(users_qs=self.scoped()['users']),
        }
        payload['count'] = len(payload['employees'])
        return Response(self.get_serializer(payload).data)


class TeamReportView(ReportBaseView):
    """One row per person across the window — the league table.

    `/reports/table/` answers "who did what on which day", which is the audit
    trail. This answers the question management asks first: across this month,
    how does each person compare. Ranked by booked value, then order count,
    then name.

    Everybody in scope appears, including people with no activity — a rep who
    booked nothing is exactly who a comparison exists to surface.

    **Query** `date_from`, `date_to` (default: the last 30 days),
    `employee`, `export=csv`

    **Responses**
    * `200` — the rollup, or a CSV attachment with `export=csv`
    * `400` — a malformed or backwards date range
    * `401` · `403` — as above
    """

    serializer_class = TeamReportSerializer
    filter_serializer_class = ReportTableFilterSerializer

    CSV_COLUMNS = [
        ('employee_code', 'Employee code'),
        ('employee_name', 'Employee'),
        ('role', 'Role'),
        ('territory', 'Territory'),
        ('days_present', 'Days present'),
        ('days_late', 'Days late'),
        ('total_working_hours', 'Working hours'),
        ('average_working_hours', 'Avg hours/day'),
        ('new_customers', 'New customers'),
        ('site_visits', 'Site visits'),
        ('site_visits_completed', 'Visits completed'),
        ('beats_assigned', 'Beats assigned'),
        ('beats_completed', 'Beats completed'),
        ('beat_completion_percentage', 'Beat completion %'),
        ('orders', 'Orders'),
        ('cancelled_orders', 'Cancelled orders'),
        ('sales', 'Sales'),
        ('average_order_value', 'Average order value'),
    ]

    def get(self, request, *args, **kwargs):
        filters = self.filters()
        qs = self.scoped()

        users, attendance = qs['users'], qs['attendance']
        visits, plans, orders = qs['visits'], qs['plans'], qs['orders']

        if filters.get('employee'):
            users = users.filter(pk=filters['employee'])
            attendance = attendance.filter(user_id=filters['employee'])
            visits = visits.filter(user_id=filters['employee'])
            plans = plans.filter(user_id=filters['employee'])
            orders = orders.filter(employee_id=filters['employee'])

        payload = services.team_rollup(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            users_qs=users,
            attendance_qs=attendance,
            customers_qs=qs['customers'],
            visits_qs=visits,
            plans_qs=plans,
            orders_qs=orders,
        )

        if self.wants_csv:
            return self.csv_response(
                filename=(
                    f'team-{payload["date_from"]}-to-{payload["date_to"]}.csv'
                ),
                rows=payload['rows'],
                columns=self.CSV_COLUMNS,
            )

        return Response(self.get_serializer(payload).data)


class VisitLogView(ReportBaseView):
    """Every site visit in the window, one row each.

    `/site-visits/` answers with the caller's own visits — right for the phone,
    where a rep is reading their own day, and no use to a manager who needs to
    see where the team went. Widening that endpoint would change what the
    mobile app receives, so this is the management view of the same records,
    scoped exactly as the rest of this module is.

    **Query** `date_from`, `date_to` (default: the last 30 days), `employee`,
    `status` (`in_progress`, `completed`, `cancelled`), `limit` (1–1000,
    default 500), `export=csv`

    **Responses**
    * `200` — the log, or a CSV attachment with `export=csv`
    * `400` — a malformed or backwards date range
    * `401` · `403` — as above
    """

    serializer_class = VisitLogSerializer
    filter_serializer_class = VisitLogFilterSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()
        visits = self.scoped()['visits']

        if filters.get('employee'):
            visits = visits.filter(user_id=filters['employee'])

        payload = services.visit_log(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            visits_qs=visits,
            status=filters.get('status'),
            limit=filters.get('limit') or (1000 if self.wants_csv else 500),
        )

        if self.wants_csv:
            return self.csv_response(
                filename=(
                    f'site-visits-{payload["date_from"]}'
                    f'-to-{payload["date_to"]}.csv'
                ),
                rows=payload['rows'],
                columns=[
                    ('date', 'Date'),
                    ('employee_code', 'Employee code'),
                    ('employee_name', 'Employee'),
                    ('site_name', 'Site'),
                    ('customer_name', 'Customer'),
                    ('purpose', 'Purpose'),
                    ('status', 'Status'),
                    ('check_in_at', 'Check in'),
                    ('check_out_at', 'Check out'),
                    ('duration_minutes', 'Minutes on site'),
                    ('check_in_distance_meters', 'Distance (m)'),
                    ('stage_observed', 'Stage observed'),
                    ('expected_order_value', 'Expected order value'),
                    ('follow_up_date', 'Follow up'),
                    ('photo_count', 'Photos'),
                    ('remarks', 'Remarks'),
                ],
            )

        return Response(self.get_serializer(payload).data)


class ReportTableView(ReportBaseView):
    """One row per person per day, for the table view.

    Built outward from attendance: a day somebody punched in is a row, with
    that person's customers, visits, beats and orders for the same day merged
    onto it. Somebody who never punched in has no row — an absence is already
    in the attendance report, and a table of empty rows for every employee
    times every day is unreadable at any real headcount.

    **Query** `date_from`, `date_to`, `employee`, `limit` (1–1000, default 500)

    `total_rows` is the count before truncation, so a client can say "showing
    500 of 3,600" rather than implying the page is everything.

    **Responses** `200` · `400` · `401` · `403`
    """

    serializer_class = ReportTableSerializer
    filter_serializer_class = ReportTableFilterSerializer

    def get(self, request, *args, **kwargs):
        filters = self.filters()
        qs = self.scoped()

        users, attendance = qs['users'], qs['attendance']
        visits, plans, orders = qs['visits'], qs['plans'], qs['orders']

        if filters.get('employee'):
            users = users.filter(pk=filters['employee'])
            attendance = attendance.filter(user_id=filters['employee'])
            visits = visits.filter(user_id=filters['employee'])
            plans = plans.filter(user_id=filters['employee'])
            orders = orders.filter(employee_id=filters['employee'])

        payload = services.report_rows(
            date_from=filters['date_from'],
            date_to=filters['date_to'],
            users_qs=users,
            attendance_qs=attendance,
            customers_qs=qs['customers'],
            visits_qs=visits,
            plans_qs=plans,
            orders_qs=orders,
            # An export is a spreadsheet somebody will filter themselves, so it
            # carries the whole window rather than the screen's page — but
            # still within the ceiling the filter enforces.
            limit=filters.get('limit') or (1000 if self.wants_csv else 500),
        )

        if self.wants_csv:
            return self.csv_response(
                filename=(
                    f'activity-{payload["date_from"]}-to-{payload["date_to"]}.csv'
                ),
                rows=payload['rows'],
                columns=[
                    ('date', 'Date'),
                    ('employee_code', 'Employee code'),
                    ('employee_name', 'Employee'),
                    ('attendance_status', 'Attendance'),
                    ('check_in_at', 'Check in'),
                    ('check_out_at', 'Check out'),
                    ('worked_hours', 'Worked hours'),
                    ('within_geofence', 'Within geofence'),
                    ('check_in_distance_meters', 'Distance (m)'),
                    ('new_customers', 'New customers'),
                    ('site_visits', 'Site visits'),
                    ('site_visits_completed', 'Visits completed'),
                    ('beats_assigned', 'Beats assigned'),
                    ('beats_completed', 'Beats completed'),
                    ('orders', 'Orders'),
                    ('sales', 'Sales'),
                ],
            )

        return Response(self.get_serializer(payload).data)
