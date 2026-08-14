"""End-to-end tests for the order endpoints.

Run against a throwaway `test_sfm_db`, like every other suite in this project.
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from customers.models import Customer, CustomerType
from products.models import Product, ProductCategory, ProductUnit

from .models import Order, OrderItem, OrderStatus

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class OrderTestCase(APITestCase):
    """Three users, one customer, three products.

    * `executive` raises orders and sees only their own.
    * `colleague` raises orders too, and must not see the executive's.
    * `manager` also holds `view_team_reports` and `cancel_orders`.
    """

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

        cls.exec_role = role('Field Executive', 'field_exec', ['place_orders'])
        cls.manager_role = role(
            'Area Manager',
            'area_manager',
            ['place_orders', 'view_team_reports', 'cancel_orders'],
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
        # No role at all: may read, may not raise an order.
        cls.outsider = User.objects.create_user(
            'sfm-0004', 'Vikram Rao', email='vikram@corp.com',
            mobile='+919876500055', password=PASSWORD,
            status=User.Status.ACTIVE,
        )

        cls.customer = Customer.objects.create(
            name='Shree Balaji Traders', contact_person='Ramesh Gupta',
            phone='9811122233', type=CustomerType.DISTRIBUTOR,
            city='New Delhi', state='Delhi', pincode='110005',
        )
        cls.other_customer = Customer.objects.create(
            name='Verma Hardware', contact_person='Sunil Verma',
            phone='9822233344', type=CustomerType.RETAILER,
            city='Noida', state='Uttar Pradesh', pincode='201301',
        )

        # 400.00 at 28% — a bag of cement.
        cls.cement = Product.objects.create(
            product_code='CEM-001', name='OPC 53 Grade Cement', brand='UltraTech',
            category=ProductCategory.CEMENT, unit=ProductUnit.BAG,
            mrp=Decimal('430.00'), selling_price=Decimal('400.00'),
            gst_percent=Decimal('28.00'), stock_quantity=500,
        )
        # 58000.00 at 18% — a tonne of steel.
        cls.steel = Product.objects.create(
            product_code='STL-001', name='TMT Bar 12mm', brand='Tata Tiscon',
            category=ProductCategory.STEEL, unit=ProductUnit.TONNE,
            mrp=Decimal('62000.00'), selling_price=Decimal('58000.00'),
            gst_percent=Decimal('18.00'), stock_quantity=40,
        )
        cls.withdrawn = Product.objects.create(
            product_code='PNT-001', name='Discontinued Primer', brand='X',
            category=ProductCategory.PAINT, unit=ProductUnit.LITRE,
            mrp=Decimal('310.00'), selling_price=Decimal('295.00'),
            gst_percent=Decimal('18.00'), stock_quantity=5, active=False,
        )

    def setUp(self):
        cache.clear()
        self.client.force_authenticate(self.executive)

    # ----------------------------------------------------------------- helpers

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})

    @property
    def list_url(self):
        return self.url('orders:list')

    def detail_url(self, pk):
        return self.url('orders:detail', pk=pk)

    def submit_url(self, pk):
        return self.url('orders:submit', pk=pk)

    def cancel_url(self, pk):
        return self.url('orders:cancel', pk=pk)

    def payload(self, **overrides):
        body = {
            'customer': str(self.customer.pk),
            'remarks': 'Deliver before the monsoon',
            'items': [{'product': str(self.cement.pk), 'quantity': 10}],
        }
        body.update(overrides)
        return body

    def make_order(self, user=None, **overrides):
        """An order through the API, so it is priced the way a real one is."""
        if user is not None:
            self.client.force_authenticate(user)
        response = self.client.post(
            self.list_url, self.payload(**overrides), format='json'
        )
        self.assertEqual(
            response.status_code, status.HTTP_201_CREATED, response.data
        )
        return Order.objects.get(pk=response.data['id'])


class CreateOrderTests(OrderTestCase):
    def test_an_order_can_be_created(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], OrderStatus.DRAFT)
        self.assertEqual(response.data['customer_name'], 'Shree Balaji Traders')
        self.assertEqual(response.data['employee_code'], 'SFM-0002')
        self.assertEqual(Order.objects.count(), 1)

    def test_an_order_number_is_generated(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        number = response.data['order_number']
        self.assertTrue(number.startswith('SO-'))
        self.assertEqual(len(number.split('-')), 3)

    def test_order_numbers_do_not_collide(self):
        """Regression guard, as on customers and products: a number derived
        from the head of a UUIDv7 key would repeat across a batch."""
        numbers = {
            self.client.post(
                self.list_url, self.payload(), format='json'
            ).data['order_number']
            for _ in range(20)
        }

        self.assertEqual(len(numbers), 20)

    def test_an_order_with_several_items(self):
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {'product': str(self.cement.pk), 'quantity': 10},
                    {'product': str(self.steel.pk), 'quantity': 2},
                ]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['item_count'], 2)
        self.assertEqual(len(response.data['items']), 2)

    def test_the_order_is_raised_in_the_callers_name(self):
        """`employee` is taken from the token, not from the payload."""
        response = self.client.post(
            self.list_url,
            {**self.payload(), 'employee': str(self.manager.pk)},
            format='json',
        )

        self.assertEqual(response.data['employee_id'], str(self.executive.pk))

    def test_the_order_date_defaults_to_today(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(
            response.data['order_date'], timezone.localdate().isoformat()
        )

    def test_a_line_carries_the_product_detail_for_rendering(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        line = response.data['items'][0]
        self.assertEqual(line['title'], 'OPC 53 Grade Cement')
        self.assertEqual(line['sku'], 'CEM-001')
        self.assertEqual(line['unit'], ProductUnit.BAG)


class PricingTests(OrderTestCase):
    def test_the_rate_comes_from_the_catalogue(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        line = response.data['items'][0]
        self.assertEqual(line['unit_price'], 400.0)
        self.assertEqual(line['gst_percent'], 28.0)

    def test_a_price_sent_by_the_client_is_ignored(self):
        """The one rule this module is built around."""
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {
                        'product': str(self.cement.pk),
                        'quantity': 10,
                        'unit_price': '1.00',
                        'gst_percent': '0.00',
                        'line_total': '1.00',
                    }
                ]
            ),
            format='json',
        )

        line = response.data['items'][0]
        self.assertEqual(line['unit_price'], 400.0)
        self.assertEqual(line['gst_percent'], 28.0)
        self.assertEqual(line['line_total'], 5120.0)

    def test_totals_sent_by_the_client_are_ignored(self):
        response = self.client.post(
            self.list_url,
            {
                **self.payload(),
                'subtotal': '1.00',
                'discount_total': '999.00',
                'gst_total': '0.00',
                'grand_total': '1.00',
            },
            format='json',
        )

        self.assertEqual(response.data['subtotal'], 4000.0)
        self.assertEqual(response.data['grand_total'], 5120.0)

    def test_the_line_arithmetic(self):
        # 10 bags at 400 = 4000; 28% GST = 1120; line total 5120.
        response = self.client.post(self.list_url, self.payload(), format='json')

        line = response.data['items'][0]
        self.assertEqual(line['gross'], 4000.0)
        self.assertEqual(line['taxable'], 4000.0)
        self.assertEqual(line['gst_amount'], 1120.0)
        self.assertEqual(line['line_total'], 5120.0)

    def test_a_discount_comes_off_before_tax(self):
        # 4000 - 500 = 3500 taxable; 28% = 980; line total 4480.
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {
                        'product': str(self.cement.pk),
                        'quantity': 10,
                        'discount': '500.00',
                    }
                ]
            ),
            format='json',
        )

        line = response.data['items'][0]
        self.assertEqual(line['taxable'], 3500.0)
        self.assertEqual(line['gst_amount'], 980.0)
        self.assertEqual(line['line_total'], 4480.0)
        self.assertEqual(response.data['discount_total'], 500.0)
        self.assertEqual(response.data['grand_total'], 4480.0)

    def test_the_order_totals_across_two_tax_rates(self):
        # Cement: 10 x 400 = 4000 @28% -> 1120
        # Steel:   2 x 58000 = 116000 @18% -> 20880
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {'product': str(self.cement.pk), 'quantity': 10},
                    {'product': str(self.steel.pk), 'quantity': 2},
                ]
            ),
            format='json',
        )

        self.assertEqual(response.data['subtotal'], 120000.0)
        self.assertEqual(response.data['discount_total'], 0.0)
        self.assertEqual(response.data['gst_total'], 22000.0)
        self.assertEqual(response.data['grand_total'], 142000.0)

    def test_the_grand_total_is_the_sum_of_the_lines(self):
        """The two ways of arriving at the figure must agree, or the rounding
        is wrong somewhere."""
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {
                        'product': str(self.cement.pk),
                        'quantity': 7,
                        'discount': '133.33',
                    },
                    {
                        'product': str(self.steel.pk),
                        'quantity': 3,
                        'discount': '1111.11',
                    },
                ]
            ),
            format='json',
        )

        lines = sum(Decimal(str(i['line_total'])) for i in response.data['items'])
        self.assertEqual(Decimal(str(response.data['grand_total'])), lines)

    def test_money_comes_back_as_numbers(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        for key in ('subtotal', 'discount_total', 'gst_total', 'grand_total'):
            self.assertIsInstance(response.data[key], float, key)

    def test_the_stored_decimals_are_exact(self):
        order = self.make_order()

        self.assertEqual(order.grand_total, Decimal('5120.00'))
        self.assertEqual(order.items.first().line_total, Decimal('5120.00'))


class ValidationTests(OrderTestCase):
    def test_an_order_needs_at_least_one_item(self):
        response = self.client.post(
            self.list_url, self.payload(items=[]), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('items', response.data)

    def test_an_unknown_customer_is_refused(self):
        response = self.client.post(
            self.list_url,
            self.payload(customer='019fd0f4-0000-0000-0000-000000000000'),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer', response.data)

    def test_an_inactive_customer_is_refused(self):
        Customer.objects.filter(pk=self.customer.pk).update(is_active=False)

        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('customer', response.data)

    def test_an_unknown_product_is_refused(self):
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {
                        'product': '019fd0f4-0000-0000-0000-000000000000',
                        'quantity': 1,
                    }
                ]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('items', response.data)

    def test_a_withdrawn_product_is_refused_by_name(self):
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[{'product': str(self.withdrawn.pk), 'quantity': 1}]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Discontinued Primer', str(response.data))

    def test_a_zero_quantity_is_refused(self):
        response = self.client.post(
            self.list_url,
            self.payload(items=[{'product': str(self.cement.pk), 'quantity': 0}]),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_negative_quantity_is_refused(self):
        response = self.client.post(
            self.list_url,
            self.payload(items=[{'product': str(self.cement.pk), 'quantity': -5}]),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_negative_discount_is_refused(self):
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {
                        'product': str(self.cement.pk),
                        'quantity': 1,
                        'discount': '-10.00',
                    }
                ]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_discount_bigger_than_the_line_is_refused(self):
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {
                        'product': str(self.cement.pk),
                        'quantity': 1,
                        'discount': '5000.00',
                    }
                ]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_same_product_twice_is_refused_with_a_readable_message(self):
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {'product': str(self.cement.pk), 'quantity': 5},
                    {'product': str(self.cement.pk), 'quantity': 5},
                ]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('twice', str(response.data))

    def test_the_database_refuses_a_zero_quantity_written_around_the_api(self):
        order = self.make_order()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderItem.objects.create(
                    order=order, product=self.steel, quantity=0,
                    unit_price=Decimal('1.00'), gst_percent=Decimal('18.00'),
                )

    def test_a_product_on_a_sold_order_cannot_be_deleted(self):
        """PROTECT: the catalogue withdraws, it does not erase."""
        self.make_order()

        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            with transaction.atomic():
                self.cement.delete()


class RollbackTests(OrderTestCase):
    def test_nothing_is_written_when_a_later_item_fails(self):
        """The whole order goes back, not just the bad line."""
        response = self.client.post(
            self.list_url,
            self.payload(
                items=[
                    {'product': str(self.cement.pk), 'quantity': 10},
                    {'product': str(self.steel.pk), 'quantity': 2},
                    {'product': str(self.withdrawn.pk), 'quantity': 1},
                ]
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(OrderItem.objects.count(), 0)

    def test_a_failed_update_leaves_the_original_lines_alone(self):
        order = self.make_order()
        original = list(order.items.values_list('product_id', 'quantity'))

        response = self.client.put(
            self.detail_url(order.pk),
            self.payload(
                items=[
                    {'product': str(self.steel.pk), 'quantity': 3},
                    {'product': str(self.withdrawn.pk), 'quantity': 1},
                ]
            ),
            format='json',
        )

        order.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            list(order.items.values_list('product_id', 'quantity')), original
        )
        self.assertEqual(order.grand_total, Decimal('5120.00'))


class UpdateTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()

    def test_a_draft_can_be_replaced_with_put(self):
        response = self.client.put(
            self.detail_url(self.order.pk),
            self.payload(
                remarks='Changed my mind',
                items=[{'product': str(self.steel.pk), 'quantity': 1}],
            ),
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['remarks'], 'Changed my mind')
        self.assertEqual(response.data['item_count'], 1)
        # 58000 @18% = 68440
        self.assertEqual(response.data['grand_total'], 68440.0)

    def test_a_patch_can_change_the_remarks_without_touching_the_lines(self):
        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'remarks': 'Call before delivery'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['item_count'], 1)
        self.assertEqual(response.data['grand_total'], 5120.0)

    def test_replacing_the_lines_reprices_the_order(self):
        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'items': [{'product': str(self.cement.pk), 'quantity': 20}]},
            format='json',
        )

        self.assertEqual(response.data['subtotal'], 8000.0)
        self.assertEqual(response.data['grand_total'], 10240.0)

    def test_the_customer_can_be_changed_on_a_draft(self):
        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'customer': str(self.other_customer.pk)},
            format='json',
        )

        self.assertEqual(response.data['customer_name'], 'Verma Hardware')

    def test_the_status_cannot_be_set_directly(self):
        """Status moves through /submit/ and /cancel/, which check the move."""
        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'status': OrderStatus.COMPLETED},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.DRAFT)


class SubmitTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()

    def test_a_draft_can_be_submitted(self):
        response = self.client.post(self.submit_url(self.order.pk), format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], OrderStatus.SUBMITTED)
        self.assertIsNotNone(response.data['submitted_at'])

    def test_submitting_reprices_against_the_catalogue_of_the_moment(self):
        """The rate on the line is what it was when the line was written; the
        order's totals are recomputed from those lines on the way through."""
        response = self.client.post(self.submit_url(self.order.pk), format='json')

        self.assertEqual(response.data['grand_total'], 5120.0)

    def test_an_empty_order_cannot_be_submitted(self):
        self.order.items.all().delete()

        response = self.client.post(self.submit_url(self.order.pk), format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_order_cannot_be_submitted_twice(self):
        self.client.post(self.submit_url(self.order.pk), format='json')

        response = self.client.post(self.submit_url(self.order.pk), format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already been submitted', str(response.data))

    def test_a_cancelled_order_cannot_be_submitted(self):
        self.client.post(
            self.cancel_url(self.order.pk),
            {'reason': 'Customer withdrew'},
            format='json',
        )

        response = self.client.post(self.submit_url(self.order.pk), format='json')

        # 400, not 403: the order is the caller's own, and what is wrong is
        # the transition, not their right to attempt it.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('cancelled order cannot be submitted', str(response.data))


class CancelTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()

    def test_a_draft_can_be_cancelled_by_its_owner(self):
        response = self.client.post(
            self.cancel_url(self.order.pk),
            {'reason': 'Customer withdrew the enquiry'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], OrderStatus.CANCELLED)
        self.assertEqual(
            response.data['cancellation_reason'], 'Customer withdrew the enquiry'
        )
        self.assertIsNotNone(response.data['cancelled_at'])

    def test_a_reason_is_required(self):
        response = self.client.post(
            self.cancel_url(self.order.pk), {}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('reason', response.data)

    def test_a_token_gesture_of_a_reason_is_refused(self):
        response = self.client.post(
            self.cancel_url(self.order.pk), {'reason': 'x'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_order_cannot_be_cancelled_twice(self):
        self.client.post(
            self.cancel_url(self.order.pk), {'reason': 'Withdrawn'}, format='json'
        )

        response = self.client.post(
            self.cancel_url(self.order.pk), {'reason': 'Again'}, format='json'
        )

        # 403 here, and deliberately so: a cancelled order is terminal, and
        # `CanActOnOrder` refuses every write to one before the view is
        # reached. Submitting is the case that had to be a 400 instead.
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_owner_cannot_cancel_their_own_submitted_order(self):
        self.client.post(self.submit_url(self.order.pk), format='json')

        response = self.client.post(
            self.cancel_url(self.order.pk),
            {'reason': 'Changed my mind'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_manager_can_cancel_a_submitted_order(self):
        self.client.post(self.submit_url(self.order.pk), format='json')
        self.client.force_authenticate(self.manager)

        response = self.client.post(
            self.cancel_url(self.order.pk),
            {'reason': 'Credit limit exceeded'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], OrderStatus.CANCELLED)


class ImmutabilityTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()

    def test_a_submitted_order_cannot_be_edited_by_its_owner(self):
        self.client.post(self.submit_url(self.order.pk), format='json')

        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'remarks': 'Sneaking a change in'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn('submitted', str(response.data).lower())

    def test_a_manager_can_amend_a_submitted_order(self):
        self.client.post(self.submit_url(self.order.pk), format='json')
        self.client.force_authenticate(self.manager)

        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'remarks': 'Corrected on the phone'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['remarks'], 'Corrected on the phone')

    def test_a_cancelled_order_cannot_be_edited_by_anybody(self):
        """Not even a manager. An order that changes after cancellation is not
        a record of anything."""
        self.client.post(
            self.cancel_url(self.order.pk), {'reason': 'Withdrawn'}, format='json'
        )
        self.client.force_authenticate(self.manager)

        response = self.client.patch(
            self.detail_url(self.order.pk),
            {'remarks': 'Reviving this'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_submitted_order_cannot_be_deleted(self):
        self.client.post(self.submit_url(self.order.pk), format='json')
        self.client.force_authenticate(self.manager)

        response = self.client.delete(self.detail_url(self.order.pk))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Order.objects.filter(pk=self.order.pk).exists())

    def test_a_draft_can_be_deleted(self):
        response = self.client.delete(self.detail_url(self.order.pk))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Order.objects.filter(pk=self.order.pk).exists())
        self.assertEqual(OrderItem.objects.count(), 0)


class AccessTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        self.mine = self.make_order(user=self.executive)
        self.theirs = self.make_order(user=self.colleague)
        self.client.force_authenticate(self.executive)

    def test_the_list_needs_a_token(self):
        self.client.force_authenticate(None)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_creating_without_a_token_is_refused(self):
        self.client.force_authenticate(None)

        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_executive_sees_only_their_own_orders(self):
        response = self.client.get(self.list_url)

        ids = {row['id'] for row in response.data['results']}
        self.assertEqual(ids, {str(self.mine.pk)})

    def test_another_persons_order_is_a_404_not_a_403(self):
        """It is not this user's to know about."""
        response = self.client.get(self.detail_url(self.theirs.pk))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_another_persons_order_cannot_be_edited(self):
        response = self.client.patch(
            self.detail_url(self.theirs.pk), {'remarks': 'Mine now'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_manager_sees_the_whole_team(self):
        self.client.force_authenticate(self.manager)

        response = self.client.get(self.list_url)

        ids = {row['id'] for row in response.data['results']}
        self.assertEqual(ids, {str(self.mine.pk), str(self.theirs.pk)})

    def test_raising_an_order_needs_the_permission(self):
        self.client.force_authenticate(self.outsider)

        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_user_without_the_permission_still_reads_an_empty_list(self):
        """Not a 403: an empty list is the truthful answer."""
        self.client.force_authenticate(self.outsider)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)


class ListTests(OrderTestCase):
    def setUp(self):
        super().setUp()
        today = timezone.localdate()

        self.first = self.make_order(user=self.executive)
        self.second = self.make_order(
            user=self.executive,
            customer=str(self.other_customer.pk),
            items=[{'product': str(self.steel.pk), 'quantity': 2}],
        )
        self.theirs = self.make_order(user=self.colleague)

        Order.objects.filter(pk=self.first.pk).update(
            order_date=today - timedelta(days=10)
        )
        Order.objects.filter(pk=self.second.pk).update(
            order_date=today - timedelta(days=2)
        )
        # A manager sees all three, which is what these are counted against.
        self.client.force_authenticate(self.manager)

    def test_the_list_is_paginated(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.data['count'], 3)
        self.assertIn('results', response.data)
        self.assertIn('next', response.data)

    def test_a_page_size_can_be_asked_for(self):
        response = self.client.get(self.list_url, {'page_size': 2})

        self.assertEqual(len(response.data['results']), 2)
        self.assertIsNotNone(response.data['next'])

    def test_a_second_page_holds_the_rest(self):
        first = self.client.get(self.list_url, {'page_size': 2})
        second = self.client.get(self.list_url, {'page_size': 2, 'page': 2})

        self.assertEqual(len(second.data['results']), 1)
        self.assertEqual(
            {r['id'] for r in first.data['results']}
            & {r['id'] for r in second.data['results']},
            set(),
        )

    # ------------------------------------------------------------------ search

    def test_search_by_order_number(self):
        response = self.client.get(
            self.list_url, {'search': self.first.order_number}
        )

        self.assertEqual(response.data['count'], 1)

    def test_search_by_customer_name(self):
        response = self.client.get(self.list_url, {'search': 'Verma'})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['customer_name'], 'Verma Hardware')

    def test_search_by_customer_code(self):
        response = self.client.get(
            self.list_url, {'search': self.other_customer.code}
        )

        self.assertEqual(response.data['count'], 1)

    def test_a_search_matching_nothing_is_an_empty_page(self):
        response = self.client.get(self.list_url, {'search': 'helicopter'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

    # ------------------------------------------------------------------ filter

    def test_filter_by_status(self):
        self.client.force_authenticate(self.executive)
        self.client.post(self.submit_url(self.first.pk), format='json')
        self.client.force_authenticate(self.manager)

        submitted = self.client.get(self.list_url, {'status': 'submitted'})
        drafts = self.client.get(self.list_url, {'status': 'draft'})

        self.assertEqual(submitted.data['count'], 1)
        self.assertEqual(drafts.data['count'], 2)

    def test_filter_by_employee(self):
        response = self.client.get(
            self.list_url, {'employee': str(self.colleague.pk)}
        )

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], str(self.theirs.pk))

    def test_filter_by_customer(self):
        response = self.client.get(
            self.list_url, {'customer': str(self.other_customer.pk)}
        )

        self.assertEqual(response.data['count'], 1)

    def test_filter_by_date_range(self):
        today = timezone.localdate()

        recent = self.client.get(
            self.list_url,
            {'date_from': (today - timedelta(days=5)).isoformat()},
        )
        old = self.client.get(
            self.list_url,
            {'date_to': (today - timedelta(days=5)).isoformat()},
        )

        self.assertEqual(recent.data['count'], 2)
        self.assertEqual(old.data['count'], 1)

    def test_a_date_range_with_both_ends(self):
        today = timezone.localdate()

        response = self.client.get(
            self.list_url,
            {
                'date_from': (today - timedelta(days=5)).isoformat(),
                'date_to': (today - timedelta(days=1)).isoformat(),
            },
        )

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['id'], str(self.second.pk))

    def test_filters_combine(self):
        response = self.client.get(
            self.list_url,
            {'status': 'draft', 'employee': str(self.executive.pk)},
        )

        self.assertEqual(response.data['count'], 2)

    # ---------------------------------------------------------------- ordering

    def test_ordering_by_order_date(self):
        response = self.client.get(self.list_url, {'ordering': 'order_date'})

        dates = [row['order_date'] for row in response.data['results']]
        self.assertEqual(dates, sorted(dates))

    def test_ordering_by_total(self):
        response = self.client.get(self.list_url, {'ordering': 'total'})

        totals = [row['grand_total'] for row in response.data['results']]
        self.assertEqual(totals, sorted(totals))

    def test_ordering_by_total_descending(self):
        response = self.client.get(self.list_url, {'ordering': '-total'})

        totals = [row['grand_total'] for row in response.data['results']]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_grand_total_is_accepted_as_the_ordering_field_too(self):
        alias = self.client.get(self.list_url, {'ordering': 'total'})
        column = self.client.get(self.list_url, {'ordering': 'grand_total'})

        self.assertEqual(
            [r['id'] for r in alias.data['results']],
            [r['id'] for r in column.data['results']],
        )

    def test_ordering_by_created_date(self):
        response = self.client.get(self.list_url, {'ordering': '-created_at'})

        self.assertEqual(response.data['results'][0]['id'], str(self.theirs.pk))

    def test_an_unknown_ordering_field_is_ignored(self):
        response = self.client.get(self.list_url, {'ordering': 'nonsense'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 3)
