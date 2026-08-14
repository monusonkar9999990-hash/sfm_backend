"""Serializers for the dashboard and the reports.

These are read-only endpoints, so the serializers do the two jobs that are
actually useful here: they validate the query string, and they write down the
shape of each response.

The query-string half matters more than it looks. Without it, `?date_from=last
tuesday` reaches the ORM and comes back as a 500, and `?date_from=2026-12-01
&date_to=2026-01-01` silently returns an empty report that reads like a bad
quarter rather than a typo.
"""

from datetime import timedelta

from django.utils import timezone
from rest_framework import serializers

# A year at a time. Not a performance guess — these are indexed aggregates —
# but a range nobody meant to ask for is usually a typo, and answering it
# slowly is worse than saying so.
MAX_RANGE_DAYS = 366

DEFAULT_RANGE_DAYS = 30


class ReportFilterSerializer(serializers.Serializer):
    """The filters every report accepts.

    Both dates are optional. Given neither, the window is the last
    `DEFAULT_RANGE_DAYS` days ending today — a report with no dates should
    answer something useful rather than scan the whole table.
    """

    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        today = timezone.localdate()

        date_to = attrs.get('date_to') or today
        date_from = attrs.get('date_from') or (
            date_to - timedelta(days=DEFAULT_RANGE_DAYS - 1)
        )

        if date_from > date_to:
            raise serializers.ValidationError(
                {'date_from': 'The start of the range is after its end.'}
            )

        if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
            raise serializers.ValidationError(
                {
                    'date_to': (
                        f'Ask for at most {MAX_RANGE_DAYS} days at a time.'
                    )
                }
            )

        attrs['date_from'] = date_from
        attrs['date_to'] = date_to
        return attrs


class SalesReportFilterSerializer(ReportFilterSerializer):
    """Sales also narrows by who sold and who bought."""

    employee_id = serializers.UUIDField(required=False)
    customer_id = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(
        required=False, min_value=1, max_value=50, default=5
    )


class AttendanceReportFilterSerializer(ReportFilterSerializer):
    """`employee` is spelled without the `_id` suffix here because that is
    what the brief asks for; sales uses `employee_id`. Both are accepted on
    both endpoints so nobody has to remember which is which."""

    employee = serializers.UUIDField(required=False)
    employee_id = serializers.UUIDField(required=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # One name wins, whichever arrived.
        attrs['employee'] = attrs.get('employee') or attrs.get('employee_id')
        return attrs


# ------------------------------------------------------------ response shapes
#
# Declared rather than inferred, so the payload is documented in code and a
# field cannot quietly disappear from a response without this file changing.


class AttendanceBlockSerializer(serializers.Serializer):
    total_employees = serializers.IntegerField()
    checked_in_today = serializers.IntegerField()
    checked_out_today = serializers.IntegerField()
    absent_today = serializers.IntegerField()
    late_arrivals = serializers.IntegerField()
    still_in = serializers.IntegerField()


class BeatBlockSerializer(serializers.Serializer):
    assigned_beats = serializers.IntegerField()
    active_beats = serializers.IntegerField()
    completed_beats = serializers.IntegerField()
    skipped_beats = serializers.IntegerField()
    skipped_stops = serializers.IntegerField()
    missed_beats = serializers.IntegerField()
    covered_stops = serializers.IntegerField()
    pending_stops = serializers.IntegerField()


class SiteVisitBlockSerializer(serializers.Serializer):
    planned_visits = serializers.IntegerField()
    recorded_visits = serializers.IntegerField()
    completed_visits = serializers.IntegerField()
    cancelled_visits = serializers.IntegerField()
    open_visits = serializers.IntegerField()


class CustomerBlockSerializer(serializers.Serializer):
    total_customers = serializers.IntegerField()
    new_customers = serializers.IntegerField()
    active_customers = serializers.IntegerField()
    inactive_customers = serializers.IntegerField()


class ProductBlockSerializer(serializers.Serializer):
    total_products = serializers.IntegerField()
    active_products = serializers.IntegerField()
    inactive_products = serializers.IntegerField()
    low_stock_products = serializers.IntegerField()
    out_of_stock_products = serializers.IntegerField()


class OrderBlockSerializer(serializers.Serializer):
    total_orders_today = serializers.IntegerField()
    submitted_orders = serializers.IntegerField()
    cancelled_orders = serializers.IntegerField()
    draft_orders = serializers.IntegerField()
    todays_sales = serializers.FloatField()
    monthly_orders = serializers.IntegerField()
    monthly_sales = serializers.FloatField()
    month_start = serializers.DateField()


class DashboardSerializer(serializers.Serializer):
    scope = serializers.ChoiceField(choices=['team', 'self'])
    date = serializers.DateField()
    generated_at = serializers.DateTimeField()
    attendance = AttendanceBlockSerializer()
    beats = BeatBlockSerializer()
    site_visits = SiteVisitBlockSerializer()
    customers = CustomerBlockSerializer()
    products = ProductBlockSerializer()
    orders = OrderBlockSerializer()


class TopCustomerSerializer(serializers.Serializer):
    customer_id = serializers.CharField()
    name = serializers.CharField()
    code = serializers.CharField()
    order_count = serializers.IntegerField()
    total = serializers.FloatField()


class TopProductSerializer(serializers.Serializer):
    product_id = serializers.CharField()
    name = serializers.CharField()
    code = serializers.CharField()
    quantity = serializers.IntegerField()
    total = serializers.FloatField()


class SalesReportSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    order_count = serializers.IntegerField()
    booked_count = serializers.IntegerField()
    cancelled_count = serializers.IntegerField()
    subtotal = serializers.FloatField()
    discount_total = serializers.FloatField()
    gst_total = serializers.FloatField()
    grand_total = serializers.FloatField()
    average_order_value = serializers.FloatField(allow_null=True)
    top_customers = TopCustomerSerializer(many=True)
    top_products = TopProductSerializer(many=True)


class AttendanceReportSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    employees = serializers.IntegerField()
    days_in_range = serializers.IntegerField()
    expected_records = serializers.IntegerField()
    present = serializers.IntegerField()
    absent = serializers.IntegerField()
    late = serializers.IntegerField()
    completed_shifts = serializers.IntegerField()
    total_working_hours = serializers.FloatField()
    average_working_hours = serializers.FloatField()


class BeatReportSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    assigned = serializers.IntegerField()
    started = serializers.IntegerField()
    completed = serializers.IntegerField()
    skipped = serializers.IntegerField()
    missed = serializers.IntegerField()
    completion_percentage = serializers.FloatField(allow_null=True)
    planned_outlets = serializers.IntegerField()
    covered_outlets = serializers.IntegerField()
    coverage_percentage = serializers.FloatField(allow_null=True)


class SiteVisitReportSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    planned = serializers.IntegerField()
    recorded = serializers.IntegerField()
    completed = serializers.IntegerField()
    cancelled = serializers.IntegerField()
    open = serializers.IntegerField()
    average_visit_minutes = serializers.FloatField(allow_null=True)


class CustomerReportSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    total_customers = serializers.IntegerField()
    new_customers = serializers.IntegerField()
    active_customers = serializers.IntegerField()
    inactive_customers = serializers.IntegerField()


class ProductReportSerializer(serializers.Serializer):
    total_products = serializers.IntegerField()
    active_products = serializers.IntegerField()
    inactive_products = serializers.IntegerField()
    low_stock_products = serializers.IntegerField()
    out_of_stock_products = serializers.IntegerField()
    low_stock_threshold = serializers.IntegerField()


class TrendDaySerializer(serializers.Serializer):
    """One calendar day in a trend series."""

    date = serializers.DateField()
    present = serializers.IntegerField()
    late = serializers.IntegerField()
    absent = serializers.IntegerField()
    new_customers = serializers.IntegerField()
    site_visits = serializers.IntegerField()
    visits_completed = serializers.IntegerField()
    beats_assigned = serializers.IntegerField()
    beats_completed = serializers.IntegerField()
    orders = serializers.IntegerField()
    # A float, not DRF's decimal-as-string: the client reads these into a
    # chart axis, and this project has been bitten once already by a string
    # arriving where a number was expected.
    sales = serializers.FloatField()


class TrendsSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    employees = serializers.IntegerField()
    days = TrendDaySerializer(many=True)


class TeamMemberSerializer(serializers.Serializer):
    """Identity and posting — what a filter control needs, and no more."""

    employee_id = serializers.CharField()
    employee_code = serializers.CharField()
    employee_name = serializers.CharField()
    role = serializers.CharField(allow_blank=True)
    territory = serializers.CharField(allow_blank=True)


class TeamDirectorySerializer(serializers.Serializer):
    scope = serializers.ChoiceField(choices=['team', 'self'])
    count = serializers.IntegerField()
    employees = TeamMemberSerializer(many=True)


class TeamRowSerializer(TeamMemberSerializer):
    """One person across the whole window."""

    days_present = serializers.IntegerField()
    days_late = serializers.IntegerField()
    days_completed = serializers.IntegerField()
    total_working_hours = serializers.FloatField()
    average_working_hours = serializers.FloatField()
    new_customers = serializers.IntegerField()
    site_visits = serializers.IntegerField()
    site_visits_completed = serializers.IntegerField()
    beats_assigned = serializers.IntegerField()
    beats_completed = serializers.IntegerField()
    beat_completion_percentage = serializers.FloatField(allow_null=True)
    orders = serializers.IntegerField()
    cancelled_orders = serializers.IntegerField()
    sales = serializers.FloatField()
    average_order_value = serializers.FloatField(allow_null=True)


class TeamReportSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    days_in_range = serializers.IntegerField()
    employees = serializers.IntegerField()
    rows = TeamRowSerializer(many=True)


class VisitLogFilterSerializer(ReportFilterSerializer):
    """The visit log narrows by person and by outcome as well as by date."""

    employee = serializers.UUIDField(required=False)
    status = serializers.ChoiceField(
        choices=['in_progress', 'completed', 'cancelled'], required=False
    )
    limit = serializers.IntegerField(required=False, min_value=1, max_value=1000)


class VisitLogRowSerializer(serializers.Serializer):
    """One site visit, flat enough to be a table row."""

    visit_id = serializers.CharField()
    date = serializers.DateField()
    employee_id = serializers.CharField()
    employee_code = serializers.CharField()
    employee_name = serializers.CharField()
    site_name = serializers.CharField()
    customer_name = serializers.CharField(allow_blank=True)
    purpose = serializers.CharField(allow_blank=True)
    status = serializers.CharField()
    check_in_at = serializers.DateTimeField()
    check_out_at = serializers.DateTimeField(allow_null=True)
    duration_minutes = serializers.IntegerField(allow_null=True)
    check_in_distance_meters = serializers.FloatField(allow_null=True)
    stage_observed = serializers.CharField(allow_blank=True)
    expected_order_value = serializers.FloatField()
    follow_up_date = serializers.DateField(allow_null=True)
    photo_count = serializers.IntegerField()
    remarks = serializers.CharField(allow_blank=True)


class VisitLogSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    total_rows = serializers.IntegerField()
    rows = VisitLogRowSerializer(many=True)


class ReportRowSerializer(serializers.Serializer):
    """One person on one day."""

    date = serializers.DateField()
    employee_id = serializers.CharField()
    employee_code = serializers.CharField()
    employee_name = serializers.CharField()
    attendance_status = serializers.CharField()
    check_in_at = serializers.DateTimeField()
    check_out_at = serializers.DateTimeField(allow_null=True)
    is_late = serializers.BooleanField()
    worked_hours = serializers.FloatField()
    check_in_distance_meters = serializers.FloatField(allow_null=True)
    within_geofence = serializers.BooleanField(allow_null=True)
    new_customers = serializers.IntegerField()
    site_visits = serializers.IntegerField()
    site_visits_completed = serializers.IntegerField()
    beats_assigned = serializers.IntegerField()
    beats_completed = serializers.IntegerField()
    beat_completion_percentage = serializers.FloatField(allow_null=True)
    orders = serializers.IntegerField()
    sales = serializers.FloatField()


class ReportTableSerializer(serializers.Serializer):
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    # Before truncation, so the client can say "showing 500 of 3,600" rather
    # than implying the page is everything.
    total_rows = serializers.IntegerField()
    rows = ReportRowSerializer(many=True)


class ReportTableFilterSerializer(ReportFilterSerializer):
    employee = serializers.UUIDField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=1000)
