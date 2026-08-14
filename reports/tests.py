"""End-to-end tests for the dashboard and the reports.

The fixture builds one real day across every module — a punch, a beat run, a
site visit, customers, products and two orders — so the figures under test are
the ones the other six modules actually write, not hand-placed rows.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from attendance.models import Attendance
from beats.models import Beat, BeatPlan, BeatPlanStatus, BeatPlanVisit
from beats.models import VisitStatus as StopStatus
from customers.models import Customer, CustomerType
from orders.models import Order, OrderItem, OrderStatus
from products.models import Product, ProductCategory, ProductUnit
from sitevisits.models import Site, SiteVisit
from sitevisits.models import VisitStatus as SiteVisitStatus

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class ReportTestCase(APITestCase):
    """A manager who sees everything, an executive who sees themselves, and an
    outsider with no reporting permission at all."""

    @classmethod
    def setUpTestData(cls):
        def role(name, code, codenames):
            r = Role.objects.create(name=name, code=code)
            r.group.permissions.set(
                Permission.objects.filter(
                    content_type__app_label='accounts', codename__in=codenames
                )
            )
            return r

        cls.exec_role = role(
            'Field Executive', 'field_exec', ['place_orders', 'view_reports']
        )
        cls.manager_role = role(
            'Area Manager',
            'area_manager',
            ['place_orders', 'view_reports', 'view_team_reports', 'cancel_orders'],
        )

        cls.executive = User.objects.create_user(
            'sfm-0002', 'Alex Mercer', email='alex@corp.com',
            mobile='+919876543210', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.exec_role,
        )
        cls.colleague = User.objects.create_user(
            'sfm-0003', 'Sneha Kulkarni', email='sneha@corp.com',
            mobile='+919876500044', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.exec_role,
        )
        cls.manager = User.objects.create_user(
            'sfm-0001', 'Rahul Deshpande', email='rahul@corp.com',
            mobile='+919876500022', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.manager_role,
        )
        cls.outsider = User.objects.create_user(
            'sfm-0004', 'Vikram Rao', email='vikram@corp.com',
            mobile='+919876500055', password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        cache.clear()
        self.client.force_authenticate(self.manager)

    # ----------------------------------------------------------------- routes

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})

    @property
    def dashboard_url(self):
        return self.url('dashboard')

    # ---------------------------------------------------------------- fixture

    def seed_day(self, day=None):
        """One day's activity, written the way the modules write it."""
        day = day or timezone.localdate()
        now = timezone.now()

        # --- attendance: executive in and out, colleague in and late
        Attendance.objects.create(
            user=self.executive, day=day,
            punch_in_at=now - timedelta(hours=8),
            punch_in_latitude=Decimal('28.612900'),
            punch_in_longitude=Decimal('77.229500'),
            punch_out_at=now, worked_minutes=480, is_late=False,
        )
        Attendance.objects.create(
            user=self.colleague, day=day,
            punch_in_at=now - timedelta(hours=3),
            punch_in_latitude=Decimal('28.612900'),
            punch_in_longitude=Decimal('77.229500'),
            is_late=True,
        )

        # --- customers
        self.customer = Customer.objects.create(
            name='Shree Balaji Traders', contact_person='Ramesh Gupta',
            phone='9811122233', type=CustomerType.DISTRIBUTOR,
            city='New Delhi', state='Delhi', pincode='110005',
        )
        self.second = Customer.objects.create(
            name='Gupta Building Material', contact_person='Anil Gupta',
            phone='9833344455', type=CustomerType.DEALER,
            city='New Delhi', state='Delhi', pincode='110005',
        )
        self.dormant = Customer.objects.create(
            name='Closed Shop', contact_person='Nobody',
            phone='9811122244', type=CustomerType.RETAILER,
            city='Noida', state='Uttar Pradesh', pincode='201301',
            is_active=False,
        )

        # --- products: one healthy, one low, one out, one withdrawn
        self.cement = Product.objects.create(
            product_code='CEM-001', name='OPC 53 Grade Cement', brand='UltraTech',
            category=ProductCategory.CEMENT, unit=ProductUnit.BAG,
            mrp=Decimal('430.00'), selling_price=Decimal('400.00'),
            gst_percent=Decimal('28.00'), stock_quantity=500,
        )
        self.steel = Product.objects.create(
            product_code='STL-001', name='TMT Bar 12mm', brand='Tata',
            category=ProductCategory.STEEL, unit=ProductUnit.TONNE,
            mrp=Decimal('62000.00'), selling_price=Decimal('58000.00'),
            gst_percent=Decimal('18.00'), stock_quantity=5,
        )
        Product.objects.create(
            product_code='PNT-001', name='Wall Primer', brand='Asian',
            category=ProductCategory.PAINT, unit=ProductUnit.LITRE,
            mrp=Decimal('310.00'), selling_price=Decimal('295.00'),
            gst_percent=Decimal('18.00'), stock_quantity=0,
        )
        Product.objects.create(
            product_code='OLD-001', name='Discontinued', brand='X',
            category=ProductCategory.OTHER, unit=ProductUnit.PIECE,
            mrp=Decimal('10.00'), selling_price=Decimal('5.00'),
            gst_percent=Decimal('18.00'), stock_quantity=3, active=False,
        )

        # --- beats: one run in progress with a covered and a skipped stop
        beat = Beat.objects.create(
            code='DEL-N-01', name='Karol Bagh North', area='Karol Bagh',
            city='New Delhi', assigned_user=self.executive, weekdays=[1, 4],
        )
        self.plan = BeatPlan.objects.create(
            beat=beat, user=self.executive, date=day,
            status=BeatPlanStatus.IN_PROGRESS, planned_outlet_count=3,
            started_at=now - timedelta(hours=4),
        )
        # One stop per customer: `BeatPlanVisit` is unique on (plan, customer),
        # because calling on the same shop twice in one run is one stop.
        stops = [
            (self.customer, StopStatus.VISITED),
            (self.second, StopStatus.SKIPPED),
            (self.dormant, StopStatus.PENDING),
        ]
        for index, (stop_customer, state) in enumerate(stops, start=1):
            BeatPlanVisit.objects.create(
                plan=self.plan, customer_ref=str(stop_customer.pk),
                customer_name=stop_customer.name, sequence=index, status=state,
            )

        # --- site visits: one closed, one still open
        site = Site.objects.create(
            code='SITE-01', name='Green Valley', customer_ref=str(self.customer.pk),
            customer_name=self.customer.name, city='Noida',
        )
        SiteVisit.objects.create(
            site=site, user=self.executive, status=SiteVisitStatus.COMPLETED,
            check_in_at=now - timedelta(hours=5),
            check_in_latitude=Decimal('28.612900'),
            check_in_longitude=Decimal('77.229500'),
            check_out_at=now - timedelta(hours=4), duration_minutes=60,
        )
        SiteVisit.objects.create(
            site=site, user=self.executive, status=SiteVisitStatus.IN_PROGRESS,
            check_in_at=now - timedelta(hours=1),
            check_in_latitude=Decimal('28.612900'),
            check_in_longitude=Decimal('77.229500'),
        )

        # --- orders: one submitted (counts), one cancelled (must not)
        self.order = self.make_order(
            self.executive, day, OrderStatus.SUBMITTED,
            [(self.cement, 10), (self.steel, 2)],
        )
        self.cancelled = self.make_order(
            self.colleague, day, OrderStatus.CANCELLED, [(self.cement, 100)]
        )
        return day

    def make_order(self, employee, day, state, lines):
        order = Order.objects.create(
            customer=self.customer, employee=employee, order_date=day, status=state
        )
        for product, quantity in lines:
            OrderItem.objects.create(
                order=order, product=product, quantity=quantity,
                unit_price=product.selling_price, gst_percent=product.gst_percent,
            )
        return order.recalculate()


class DashboardTests(ReportTestCase):
    def test_the_dashboard_answers(self):
        self.seed_day()

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for block in (
            'attendance', 'beats', 'site_visits', 'customers', 'products', 'orders'
        ):
            self.assertIn(block, response.data, block)

    def test_the_attendance_block(self):
        self.seed_day()

        block = self.client.get(self.dashboard_url).data['attendance']

        self.assertEqual(block['total_employees'], 4)
        self.assertEqual(block['checked_in_today'], 2)
        self.assertEqual(block['checked_out_today'], 1)
        self.assertEqual(block['absent_today'], 2)
        self.assertEqual(block['late_arrivals'], 1)
        self.assertEqual(block['still_in'], 1)

    def test_the_beats_block(self):
        self.seed_day()

        block = self.client.get(self.dashboard_url).data['beats']

        self.assertEqual(block['assigned_beats'], 1)
        self.assertEqual(block['active_beats'], 1)
        self.assertEqual(block['completed_beats'], 0)
        self.assertEqual(block['covered_stops'], 1)
        self.assertEqual(block['skipped_stops'], 1)
        self.assertEqual(block['pending_stops'], 1)

    def test_the_site_visits_block(self):
        self.seed_day()

        block = self.client.get(self.dashboard_url).data['site_visits']

        self.assertEqual(block['recorded_visits'], 2)
        self.assertEqual(block['completed_visits'], 1)
        self.assertEqual(block['open_visits'], 1)
        self.assertEqual(block['cancelled_visits'], 0)

    def test_the_customers_block(self):
        self.seed_day()

        block = self.client.get(self.dashboard_url).data['customers']

        self.assertEqual(block['total_customers'], 3)
        self.assertEqual(block['active_customers'], 2)
        self.assertEqual(block['new_customers'], 3)

    def test_the_products_block(self):
        self.seed_day()

        block = self.client.get(self.dashboard_url).data['products']

        self.assertEqual(block['total_products'], 4)
        self.assertEqual(block['active_products'], 3)
        # 5 units, under the threshold of 25 and above zero.
        self.assertEqual(block['low_stock_products'], 1)
        self.assertEqual(block['out_of_stock_products'], 1)

    def test_the_orders_block_excludes_cancelled_revenue(self):
        self.seed_day()

        block = self.client.get(self.dashboard_url).data['orders']

        # Cement 10 x 400 @28% = 5120; steel 2 x 58000 @18% = 136880.
        self.assertEqual(block['total_orders_today'], 2)
        self.assertEqual(block['submitted_orders'], 1)
        self.assertEqual(block['cancelled_orders'], 1)
        self.assertEqual(block['todays_sales'], 142000.0)
        self.assertEqual(block['monthly_sales'], 142000.0)

    def test_an_empty_database_answers_zeroes_not_an_error(self):
        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['orders']['todays_sales'], 0)
        self.assertEqual(response.data['customers']['total_customers'], 0)
        self.assertEqual(response.data['beats']['assigned_beats'], 0)
        self.assertEqual(response.data['attendance']['checked_in_today'], 0)
        # Four users exist even with no activity recorded.
        self.assertEqual(response.data['attendance']['total_employees'], 4)


class ScopeTests(ReportTestCase):
    def test_a_manager_sees_the_organisation(self):
        self.seed_day()

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.data['scope'], 'team')
        self.assertEqual(response.data['orders']['total_orders_today'], 2)

    def test_an_executive_sees_only_their_own(self):
        self.seed_day()
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.data['scope'], 'self')
        # The colleague's cancelled order is not theirs to see.
        self.assertEqual(response.data['orders']['total_orders_today'], 1)
        self.assertEqual(response.data['orders']['cancelled_orders'], 0)
        self.assertEqual(response.data['attendance']['total_employees'], 1)

    def test_master_data_is_not_narrowed_by_who_is_asking(self):
        """Everybody sells from the same catalogue to the same book."""
        self.seed_day()
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.data['products']['total_products'], 4)
        self.assertEqual(response.data['customers']['total_customers'], 3)


class AccessTests(ReportTestCase):
    def test_the_dashboard_needs_a_token(self):
        self.client.force_authenticate(None)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_dashboard_needs_the_reports_permission(self):
        self.client.force_authenticate(self.outsider)

        response = self.client.get(self.dashboard_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_every_report_needs_a_token(self):
        self.client.force_authenticate(None)

        for name in (
            'reports:sales', 'reports:attendance', 'reports:beats',
            'reports:site-visits', 'reports:customers', 'reports:products',
        ):
            with self.subTest(report=name):
                response = self.client.get(self.url(name))
                self.assertEqual(
                    response.status_code, status.HTTP_401_UNAUTHORIZED
                )

    def test_every_report_needs_the_permission(self):
        self.client.force_authenticate(self.outsider)

        for name in (
            'reports:sales', 'reports:attendance', 'reports:beats',
            'reports:site-visits', 'reports:customers', 'reports:products',
        ):
            with self.subTest(report=name):
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_endpoints_are_read_only(self):
        self.seed_day()

        for method in (self.client.post, self.client.put, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(self.dashboard_url, {}, format='json')
                self.assertEqual(
                    response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED
                )


class SalesReportTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.report_url = self.url('reports:sales')

    def test_the_totals(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['order_count'], 2)
        self.assertEqual(response.data['booked_count'], 1)
        self.assertEqual(response.data['cancelled_count'], 1)
        self.assertEqual(response.data['subtotal'], 120000.0)
        self.assertEqual(response.data['gst_total'], 22000.0)
        self.assertEqual(response.data['grand_total'], 142000.0)

    def test_the_average_order_value_ignores_cancellations(self):
        response = self.client.get(self.report_url)

        # One booked order, so the average is that order.
        self.assertEqual(response.data['average_order_value'], 142000.0)

    def test_top_customers(self):
        response = self.client.get(self.report_url)

        top = response.data['top_customers']
        self.assertEqual(len(top), 1)
        self.assertEqual(top[0]['name'], 'Shree Balaji Traders')
        self.assertEqual(top[0]['total'], 142000.0)

    def test_top_products(self):
        response = self.client.get(self.report_url)

        names = [row['name'] for row in response.data['top_products']]
        self.assertEqual(names[0], 'TMT Bar 12mm')
        self.assertEqual(len(response.data['top_products']), 2)

    def test_the_leaderboard_size_can_be_asked_for(self):
        response = self.client.get(self.report_url, {'limit': 1})

        self.assertEqual(len(response.data['top_products']), 1)

    def test_filter_by_employee(self):
        mine = self.client.get(
            self.report_url, {'employee_id': str(self.executive.pk)}
        )
        theirs = self.client.get(
            self.report_url, {'employee_id': str(self.colleague.pk)}
        )

        self.assertEqual(mine.data['booked_count'], 1)
        # The colleague's only order was cancelled.
        self.assertEqual(theirs.data['booked_count'], 0)
        self.assertEqual(theirs.data['grand_total'], 0)

    def test_filter_by_customer(self):
        response = self.client.get(
            self.report_url, {'customer_id': str(self.customer.pk)}
        )

        self.assertEqual(response.data['order_count'], 2)

    def test_filter_by_a_customer_with_no_orders(self):
        response = self.client.get(
            self.report_url, {'customer_id': str(self.dormant.pk)}
        )

        self.assertEqual(response.data['order_count'], 0)
        self.assertEqual(response.data['grand_total'], 0)
        self.assertIsNone(response.data['average_order_value'])

    def test_a_date_range_that_excludes_everything(self):
        past = self.day - timedelta(days=200)

        response = self.client.get(
            self.report_url,
            {'date_from': past.isoformat(), 'date_to': past.isoformat()},
        )

        self.assertEqual(response.data['order_count'], 0)
        self.assertEqual(response.data['top_customers'], [])

    def test_a_backwards_date_range_is_refused(self):
        response = self.client.get(
            self.report_url,
            {'date_from': '2026-12-01', 'date_to': '2026-01-01'},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('date_from', response.data)

    def test_a_malformed_date_is_refused(self):
        response = self.client.get(self.report_url, {'date_from': 'last tuesday'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_absurd_range_is_refused(self):
        response = self.client.get(
            self.report_url,
            {'date_from': '2020-01-01', 'date_to': '2026-01-01'},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_executive_sees_only_their_own_sales(self):
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.report_url)

        self.assertEqual(response.data['order_count'], 1)


class AttendanceReportTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.report_url = self.url('reports:attendance')

    def test_the_summary(self):
        response = self.client.get(
            self.report_url,
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )

        self.assertEqual(response.data['present'], 2)
        self.assertEqual(response.data['late'], 1)
        self.assertEqual(response.data['employees'], 4)
        self.assertEqual(response.data['days_in_range'], 1)
        self.assertEqual(response.data['expected_records'], 4)
        self.assertEqual(response.data['absent'], 2)

    def test_average_working_hours(self):
        response = self.client.get(
            self.report_url,
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )

        # One closed shift of 480 minutes.
        self.assertEqual(response.data['average_working_hours'], 8.0)
        self.assertEqual(response.data['total_working_hours'], 8.0)
        self.assertEqual(response.data['completed_shifts'], 1)

    def test_filter_by_employee(self):
        response = self.client.get(
            self.report_url,
            {
                'employee': str(self.colleague.pk),
                'date_from': self.day.isoformat(),
                'date_to': self.day.isoformat(),
            },
        )

        self.assertEqual(response.data['employees'], 1)
        self.assertEqual(response.data['present'], 1)
        self.assertEqual(response.data['late'], 1)
        self.assertEqual(response.data['absent'], 0)

    def test_employee_id_is_accepted_as_well(self):
        by_name = self.client.get(
            self.report_url, {'employee': str(self.colleague.pk)}
        )
        by_id = self.client.get(
            self.report_url, {'employee_id': str(self.colleague.pk)}
        )

        self.assertEqual(by_name.data['present'], by_id.data['present'])

    def test_an_empty_range_reports_no_hours_rather_than_failing(self):
        past = self.day - timedelta(days=100)

        response = self.client.get(
            self.report_url,
            {'date_from': past.isoformat(), 'date_to': past.isoformat()},
        )

        self.assertEqual(response.data['present'], 0)
        self.assertEqual(response.data['average_working_hours'], 0)


class BeatReportTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.report_url = self.url('reports:beats')

    def test_the_summary(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['assigned'], 1)
        self.assertEqual(response.data['started'], 1)
        self.assertEqual(response.data['completed'], 0)
        self.assertEqual(response.data['skipped'], 1)
        self.assertEqual(response.data['completion_percentage'], 0.0)

    def test_coverage_against_the_planned_outlets(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['planned_outlets'], 3)
        self.assertEqual(response.data['covered_outlets'], 1)
        self.assertAlmostEqual(response.data['coverage_percentage'], 33.33, places=2)

    def test_completion_of_no_beats_is_null_not_zero(self):
        past = self.day - timedelta(days=100)

        response = self.client.get(
            self.report_url,
            {'date_from': past.isoformat(), 'date_to': past.isoformat()},
        )

        self.assertEqual(response.data['assigned'], 0)
        self.assertIsNone(response.data['completion_percentage'])

    def test_a_completed_run_moves_the_percentage(self):
        BeatPlan.objects.filter(pk=self.plan.pk).update(
            status=BeatPlanStatus.COMPLETED
        )

        response = self.client.get(self.report_url)

        self.assertEqual(response.data['completed'], 1)
        self.assertEqual(response.data['completion_percentage'], 100.0)


class SiteVisitReportTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.report_url = self.url('reports:site-visits')

    def test_the_summary(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['recorded'], 2)
        self.assertEqual(response.data['completed'], 1)
        self.assertEqual(response.data['cancelled'], 0)
        self.assertEqual(response.data['open'], 1)

    def test_planned_mirrors_recorded_because_there_is_no_planning_step(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['planned'], response.data['recorded'])

    def test_average_duration_counts_only_closed_visits(self):
        response = self.client.get(self.report_url)

        # One closed visit of 60 minutes; the open one has no duration yet.
        self.assertEqual(response.data['average_visit_minutes'], 60.0)

    def test_an_empty_range_has_no_average(self):
        past = self.day - timedelta(days=100)

        response = self.client.get(
            self.report_url,
            {'date_from': past.isoformat(), 'date_to': past.isoformat()},
        )

        self.assertEqual(response.data['recorded'], 0)
        self.assertIsNone(response.data['average_visit_minutes'])


class CustomerReportTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.report_url = self.url('reports:customers')

    def test_the_summary(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['total_customers'], 3)
        self.assertEqual(response.data['active_customers'], 2)
        self.assertEqual(response.data['inactive_customers'], 1)
        self.assertEqual(response.data['new_customers'], 3)

    def test_new_customers_respects_the_window(self):
        past = self.day - timedelta(days=100)

        response = self.client.get(
            self.report_url,
            {'date_from': past.isoformat(), 'date_to': past.isoformat()},
        )

        self.assertEqual(response.data['new_customers'], 0)
        # The book itself is not date-filtered.
        self.assertEqual(response.data['total_customers'], 3)


class ProductReportTests(ReportTestCase):
    def setUp(self):
        super().setUp()
        self.seed_day()
        self.report_url = self.url('reports:products')

    def test_the_summary(self):
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['total_products'], 4)
        self.assertEqual(response.data['active_products'], 3)
        self.assertEqual(response.data['inactive_products'], 1)
        self.assertEqual(response.data['low_stock_products'], 1)
        self.assertEqual(response.data['out_of_stock_products'], 1)

    def test_the_threshold_is_reported_with_the_count(self):
        """A "low stock" figure means nothing without the line it is under."""
        response = self.client.get(self.report_url)

        self.assertEqual(response.data['low_stock_threshold'], 25)

    def test_an_empty_catalogue(self):
        # The orders go first: `OrderItem.product` is PROTECT, so a product
        # that has been sold cannot be deleted. That is the catalogue working
        # as intended, not something to work around here.
        OrderItem.objects.all().delete()
        Order.objects.all().delete()
        Product.objects.all().delete()

        response = self.client.get(self.report_url)

        self.assertEqual(response.data['total_products'], 0)
        self.assertEqual(response.data['low_stock_products'], 0)


class QueryCountTests(ReportTestCase):
    """The dashboard is the front page — it loads on every app start.

    These ceilings are what stop a well-meaning `for` loop turning six
    aggregates into six hundred queries. They are deliberately a little above
    the current count so a legitimate extra join does not fail the build, but
    close enough that an N+1 does.
    """

    def test_the_dashboard_is_a_fixed_number_of_queries(self):
        """A cold request: nothing cached, permissions not yet resolved.

        Twelve rather than the earlier eleven since the administration module
        landed — its maintenance gate reads a cached flag, and a cold cache
        costs the one lookup. Warm, it costs nothing, which is what the test
        below measures.
        """
        self.seed_day()

        with self.assertNumQueries(12):
            self.client.get(self.dashboard_url)

    def test_the_dashboard_does_not_grow_with_the_data(self):
        """The real N+1 test: ten times the rows, the same query count.

        The first request of a test is warmed up and thrown away. Django
        caches a user's permissions on the instance after the first check, so
        a cold request runs two queries a warm one does not — comparing one
        against the other would measure that, not the data.
        """
        self.seed_day()
        self.client.get(self.dashboard_url)

        with self.assertNumQueries(9):
            self.client.get(self.dashboard_url)

        for index in range(10):
            customer = Customer.objects.create(
                name=f'Shop {index}', contact_person='Someone',
                phone=f'98111333{index:02d}', type=CustomerType.RETAILER,
                city='Noida', state='Uttar Pradesh', pincode='201301',
            )
            order = Order.objects.create(
                customer=customer, employee=self.executive,
                order_date=timezone.localdate(), status=OrderStatus.SUBMITTED,
            )
            OrderItem.objects.create(
                order=order, product=self.cement, quantity=index + 1,
                unit_price=self.cement.selling_price,
                gst_percent=self.cement.gst_percent,
            )
            order.recalculate()

        with self.assertNumQueries(9):
            self.client.get(self.dashboard_url)

    def test_the_sales_report_does_not_grow_with_the_data(self):
        self.seed_day()
        url = self.url('reports:sales')
        self.client.get(url)

        with self.assertNumQueries(3):
            self.client.get(url)

        for index in range(10):
            order = Order.objects.create(
                customer=self.customer, employee=self.executive,
                order_date=timezone.localdate(), status=OrderStatus.SUBMITTED,
            )
            OrderItem.objects.create(
                order=order, product=self.steel, quantity=index + 1,
                unit_price=self.steel.selling_price,
                gst_percent=self.steel.gst_percent,
            )
            order.recalculate()

        with self.assertNumQueries(3):
            self.client.get(url)


class TrendsTests(ReportTestCase):
    """Per-day series for the charts.

    Every other report answers "how many across this window"; a chart needs a
    point per day, and nothing produced that shape before this endpoint.
    """

    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.trends_url = self.url('reports:trends')

    def test_one_row_per_calendar_day(self):
        start = self.day - timedelta(days=4)

        response = self.client.get(
            self.trends_url,
            {'date_from': start.isoformat(), 'date_to': self.day.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['days']), 5)
        self.assertEqual(
            [str(row['date']) for row in response.data['days']],
            [(start + timedelta(days=n)).isoformat() for n in range(5)],
        )

    def test_a_day_with_no_activity_is_present_with_zeros(self):
        quiet = self.day - timedelta(days=3)

        response = self.client.get(
            self.trends_url,
            {'date_from': quiet.isoformat(), 'date_to': quiet.isoformat()},
        )

        # Omitting empty days would draw a line straight through them, which
        # says work continued when it did not.
        row = response.data['days'][0]
        self.assertEqual(row['present'], 0)
        self.assertEqual(row['new_customers'], 0)
        self.assertEqual(row['orders'], 0)
        self.assertEqual(row['sales'], 0.0)

    def test_customers_are_counted_on_the_day_they_were_created(self):
        """The regression that made this endpoint report nothing.

        `TruncDate` compiles to `CONVERT_TZ` on MySQL and returns NULL unless
        the server's timezone tables are loaded, so every customer grouped
        under NULL and every day reported zero — no error, just a chart saying
        nobody had done anything. The day is worked out in Python instead.
        """
        response = self.client.get(
            self.trends_url,
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )

        today = response.data['days'][0]
        self.assertEqual(today['new_customers'], 3)
        # And it agrees with the window report over the same range, which is
        # the cross-check that catches a bucketing error in either.
        window = self.client.get(
            self.url('reports:customers'),
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )
        self.assertEqual(today['new_customers'], window.data['new_customers'])

    def test_the_series_totals_match_the_window_report(self):
        start = self.day - timedelta(days=6)
        params = {'date_from': start.isoformat(), 'date_to': self.day.isoformat()}

        trends = self.client.get(self.trends_url, params).data
        attendance = self.client.get(self.url('reports:attendance'), params).data

        self.assertEqual(
            sum(row['present'] for row in trends['days']),
            attendance['present'],
        )
        self.assertEqual(
            sum(row['late'] for row in trends['days']),
            attendance['late'],
        )

    def test_cancelled_orders_are_counted_but_not_billed(self):
        response = self.client.get(
            self.trends_url,
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )

        today = response.data['days'][0]
        sales = self.client.get(
            self.url('reports:sales'),
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        ).data

        # Same rule the sales report applies: a called-off order is not revenue.
        self.assertEqual(today['sales'], sales['grand_total'])

    def test_it_needs_the_reports_permission(self):
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.get(self.trends_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_a_backwards_range_is_refused(self):
        response = self.client.get(
            self.trends_url,
            {
                'date_from': self.day.isoformat(),
                'date_to': (self.day - timedelta(days=5)).isoformat(),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReportTableTests(ReportTestCase):
    """One row per person per day, for the table view."""

    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.table_url = self.url('reports:table')

    def test_a_row_carries_the_whole_day_for_one_person(self):
        response = self.client.get(
            self.table_url,
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['rows'])

        row = response.data['rows'][0]
        for field in (
            'date', 'employee_code', 'employee_name', 'attendance_status',
            'check_in_at', 'check_out_at', 'is_late', 'worked_hours',
            'new_customers', 'site_visits', 'beats_assigned',
            'beats_completed', 'orders', 'sales',
        ):
            self.assertIn(field, row)

    def test_status_is_derived_because_there_is_no_column_for_it(self):
        response = self.client.get(
            self.table_url,
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        )

        for row in response.data['rows']:
            expected = 'late' if row['is_late'] else 'present'
            self.assertEqual(row['attendance_status'], expected)

    def test_only_days_somebody_punched_in_become_rows(self):
        # Absence is already in the attendance report. A row per employee per
        # day would be mostly empty and unreadable at any real headcount.
        quiet = self.day - timedelta(days=30)

        response = self.client.get(
            self.table_url,
            {'date_from': quiet.isoformat(), 'date_to': quiet.isoformat()},
        )

        self.assertEqual(response.data['rows'], [])
        self.assertEqual(response.data['total_rows'], 0)

    def test_the_count_before_truncation_is_reported(self):
        response = self.client.get(
            self.table_url,
            {
                'date_from': (self.day - timedelta(days=7)).isoformat(),
                'date_to': self.day.isoformat(),
                'limit': 1,
            },
        )

        # "Showing 1 of N" is honest; a silently truncated page is not.
        self.assertEqual(len(response.data['rows']), 1)
        self.assertGreaterEqual(response.data['total_rows'], 1)

    def test_it_needs_the_reports_permission(self):
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.get(self.table_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class TeamDirectoryTests(ReportTestCase):
    """Who the caller may ask about, for a filter control."""

    def setUp(self):
        super().setUp()
        self.directory_url = self.url('reports:employees')

    def test_a_manager_sees_the_whole_active_roster(self):
        response = self.client.get(self.directory_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['scope'], 'team')

        codes = {row['employee_code'] for row in response.data['employees']}
        self.assertEqual(
            codes, {'SFM-0001', 'SFM-0002', 'SFM-0003', 'SFM-0004'}
        )
        self.assertEqual(response.data['count'], 4)

    def test_an_executive_sees_only_themselves(self):
        self.client.force_authenticate(self.executive)
        response = self.client.get(self.directory_url)

        self.assertEqual(response.data['scope'], 'self')
        self.assertEqual(
            [row['employee_code'] for row in response.data['employees']],
            ['SFM-0002'],
        )

    def test_it_carries_no_personal_data(self):
        # The whole reason this exists rather than reusing /admin/employees/:
        # a supervisor may need the names without being handed the staff file.
        row = self.client.get(self.directory_url).data['employees'][0]

        self.assertEqual(
            set(row),
            {'employee_id', 'employee_code', 'employee_name', 'role', 'territory'},
        )

    def test_it_needs_the_reports_permission(self):
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.get(self.directory_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class TeamRollupTests(ReportTestCase):
    """One row per person across the window — the league table."""

    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.team_url = self.url('reports:team')

    def window(self, **extra):
        return self.client.get(
            self.team_url,
            {
                'date_from': self.day.isoformat(),
                'date_to': self.day.isoformat(),
                **extra,
            },
        )

    def row_for(self, response, code):
        return next(
            row for row in response.data['rows'] if row['employee_code'] == code
        )

    def test_everybody_in_scope_gets_a_row_even_with_nothing_to_show(self):
        # A rep who booked nothing is exactly who a comparison exists to
        # surface, so a quiet week must not drop off the table.
        response = self.window()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['employees'], 4)

        idle = self.row_for(response, 'SFM-0004')
        self.assertEqual(idle['orders'], 0)
        self.assertEqual(idle['sales'], 0)
        self.assertEqual(idle['days_present'], 0)

    def test_a_row_adds_up_that_persons_window(self):
        response = self.window()
        mine = self.row_for(response, 'SFM-0002')

        self.assertEqual(mine['days_present'], 1)
        self.assertEqual(mine['days_late'], 0)
        self.assertEqual(mine['days_completed'], 1)
        self.assertEqual(mine['total_working_hours'], 8.0)
        self.assertEqual(mine['site_visits'], 2)
        self.assertEqual(mine['site_visits_completed'], 1)
        self.assertEqual(mine['beats_assigned'], 1)
        self.assertEqual(mine['beats_completed'], 0)
        self.assertEqual(mine['beat_completion_percentage'], 0.0)
        self.assertEqual(mine['orders'], 1)
        self.assertGreater(mine['sales'], 0)
        self.assertEqual(mine['average_order_value'], mine['sales'])

    def test_a_late_punch_is_counted_as_late_and_still_present(self):
        late = self.row_for(self.window(), 'SFM-0003')

        self.assertEqual(late['days_present'], 1)
        self.assertEqual(late['days_late'], 1)
        # Punched in but never out, so no shift was completed.
        self.assertEqual(late['days_completed'], 0)

    def test_a_cancelled_order_is_counted_but_is_not_revenue(self):
        cancelled = self.row_for(self.window(), 'SFM-0003')

        self.assertEqual(cancelled['cancelled_orders'], 1)
        self.assertEqual(cancelled['orders'], 0)
        self.assertEqual(cancelled['sales'], 0)

    def test_the_table_is_ranked_by_what_management_ranks_by(self):
        rows = self.window().data['rows']

        self.assertEqual(rows[0]['employee_code'], 'SFM-0002')
        self.assertEqual(
            [row['sales'] for row in rows],
            sorted((row['sales'] for row in rows), reverse=True),
        )

    def test_the_total_matches_the_sales_report_for_the_same_window(self):
        # Two endpoints reading the same orders must not disagree, which is the
        # sort of thing nobody notices until a review meeting.
        team = sum(row['sales'] for row in self.window().data['rows'])
        sales = self.client.get(
            self.url('reports:sales'),
            {'date_from': self.day.isoformat(), 'date_to': self.day.isoformat()},
        ).data['grand_total']

        self.assertAlmostEqual(team, sales, places=2)

    def test_an_executive_sees_only_their_own_row(self):
        self.client.force_authenticate(self.executive)
        response = self.window()

        self.assertEqual(
            [row['employee_code'] for row in response.data['rows']], ['SFM-0002']
        )

    def test_one_employee_can_be_singled_out(self):
        response = self.window(employee=str(self.colleague.pk))

        self.assertEqual(
            [row['employee_code'] for row in response.data['rows']], ['SFM-0003']
        )

    def test_it_exports_a_spreadsheet(self):
        response = self.window(export='csv')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        self.assertIn('attachment;', response['Content-Disposition'])

        body = response.content.decode('utf-8-sig')
        header, *rows = body.strip().splitlines()
        self.assertTrue(header.startswith('Employee code,Employee,Role'))
        self.assertEqual(len(rows), 4)

    def test_it_needs_the_reports_permission(self):
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.get(self.team_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class VisitLogTests(ReportTestCase):
    """The team's site visits as rows, for a desk rather than a phone."""

    def setUp(self):
        super().setUp()
        self.day = self.seed_day()
        self.log_url = self.url('reports:visit-log')

    def window(self, **extra):
        return self.client.get(
            self.log_url,
            {
                'date_from': self.day.isoformat(),
                'date_to': self.day.isoformat(),
                **extra,
            },
        )

    def test_a_manager_sees_visits_that_are_not_their_own(self):
        # The whole point: `/site-visits/` would answer with the manager's own
        # visits, which is an empty list here.
        response = self.window()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total_rows'], 2)
        self.assertEqual(
            {row['employee_code'] for row in response.data['rows']}, {'SFM-0002'}
        )

    def test_a_row_carries_what_a_table_needs(self):
        row = self.window().data['rows'][0]

        for field in (
            'date', 'employee_name', 'site_name', 'customer_name', 'status',
            'check_in_at', 'check_out_at', 'duration_minutes', 'photo_count',
        ):
            self.assertIn(field, row)

        self.assertEqual(row['site_name'], 'Green Valley')

    def test_it_narrows_by_outcome(self):
        response = self.window(status='completed')

        self.assertEqual(response.data['total_rows'], 1)
        self.assertEqual(response.data['rows'][0]['duration_minutes'], 60)

    def test_it_narrows_to_one_person(self):
        self.assertEqual(
            self.window(employee=str(self.colleague.pk)).data['total_rows'], 0
        )

    def test_an_executive_still_sees_only_their_own(self):
        self.client.force_authenticate(self.colleague)

        self.assertEqual(self.window().data['total_rows'], 0)

    def test_it_exports_a_spreadsheet(self):
        response = self.window(export='csv')

        self.assertTrue(response['Content-Type'].startswith('text/csv'))
        body = response.content.decode('utf-8-sig').strip().splitlines()
        self.assertTrue(body[0].startswith('Date,Employee code,Employee,Site'))
        self.assertEqual(len(body), 3)

    def test_it_needs_the_reports_permission(self):
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.get(self.log_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )
