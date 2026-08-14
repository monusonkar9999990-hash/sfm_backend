"""End-to-end tests for the offline sync endpoints.

These go through the real thing: a record uploaded here is written by the
module that owns it, through that module's own endpoint, with that module's
validation and permissions. A test that passes means an offline order really
did become an `orders.Order` — not that a mock was called.
"""

import io
import json
from datetime import timedelta
from decimal import Decimal

from PIL import Image
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from attendance.models import Attendance
from beats.models import Beat, BeatOutlet, BeatPlan, BeatPlanStatus, BeatPlanVisit
from beats.models import VisitStatus as StopStatus
from customers.models import Customer, CustomerType
from orders.models import Order, OrderStatus
from products.models import Product, ProductCategory, ProductUnit
from sitevisits.models import Site, SiteVisit

from .models import BatchStatus, RecordStatus, SyncBatch, SyncDownloadLog, SyncRecord

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class SyncTestCase(APITestCase):
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
            'Field Executive',
            'field_exec',
            [
                'place_orders',
                'onboard_customers',
                'mark_attendance',
                'plan_beats',
                'log_site_visits',
            ],
        )
        # No business permissions at all: authenticated, but allowed nothing.
        cls.limited_role = role('Observer', 'observer', [])

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
        cls.observer = User.objects.create_user(
            'sfm-0004', 'Vikram Rao', email='vikram@corp.com',
            mobile='+919876500055', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.limited_role,
        )

        cls.customer = Customer.objects.create(
            name='Shree Balaji Traders', contact_person='Ramesh Gupta',
            phone='9811122233', type=CustomerType.DISTRIBUTOR,
            city='New Delhi', state='Delhi', pincode='110005',
        )
        cls.cement = Product.objects.create(
            product_code='CEM-001', name='OPC 53 Grade Cement', brand='UltraTech',
            category=ProductCategory.CEMENT, unit=ProductUnit.BAG,
            mrp=Decimal('430.00'), selling_price=Decimal('400.00'),
            gst_percent=Decimal('28.00'), stock_quantity=500,
        )

    def setUp(self):
        cache.clear()
        self.client.force_authenticate(self.executive)

    # ----------------------------------------------------------------- routes

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})

    @property
    def upload_url(self):
        return self.url('sync:upload')

    @property
    def download_url(self):
        return self.url('sync:download')

    @property
    def status_url(self):
        return self.url('sync:status')

    # ---------------------------------------------------------------- payloads

    def order_record(self, local_id='local-order-1', quantity=10):
        return {
            'entity_type': 'orders',
            'local_id': local_id,
            'operation': 'create',
            'device_timestamp': timezone.now().isoformat(),
            'payload': {
                'customer': str(self.customer.pk),
                'remarks': 'Booked with no signal',
                'items': [
                    {'product': str(self.cement.pk), 'quantity': quantity}
                ],
            },
        }

    def customer_record(self, local_id='local-cust-1', phone='9822233344'):
        return {
            'entity_type': 'customers',
            'local_id': local_id,
            'operation': 'create',
            'payload': {
                'name': f'Verma Hardware {phone}',
                'contact_person': 'Sunil Verma',
                'phone': phone,
                'type': 'retailer',
                'address': 'Shop 8',
                'city': 'Noida',
                'state': 'Uttar Pradesh',
                'pincode': '201301',
            },
        }

    def upload(self, records, key='batch-1', **extra):
        return self.client.post(
            self.upload_url,
            {'idempotency_key': key, 'device_id': 'pixel-7a', 'records': records,
             **extra},
            format='json',
        )


class UploadBatchTests(SyncTestCase):
    def test_a_batch_is_applied(self):
        response = self.upload([self.order_record(), self.customer_record()])

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['summary']['applied'], 2)
        self.assertEqual(response.data['summary']['failed'], 0)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 2)

    def test_the_server_id_comes_back_for_each_local_id(self):
        response = self.upload([self.order_record(local_id='abc-123')])

        result = response.data['results'][0]
        self.assertEqual(result['local_id'], 'abc-123')
        self.assertTrue(result['server_id'])
        self.assertTrue(Order.objects.filter(pk=result['server_id']).exists())

    def test_the_order_is_priced_by_the_server_not_the_device(self):
        """The record goes through the real endpoint, so the real rules run."""
        record = self.order_record()
        record['payload']['items'][0]['unit_price'] = '1.00'
        record['payload']['grand_total'] = '1.00'

        response = self.upload([record])

        order = Order.objects.get(pk=response.data['results'][0]['server_id'])
        self.assertEqual(order.grand_total, Decimal('5120.00'))

    def test_the_order_is_raised_in_the_uploading_users_name(self):
        response = self.upload([self.order_record()])

        order = Order.objects.get(pk=response.data['results'][0]['server_id'])
        self.assertEqual(order.employee, self.executive)

    def test_a_batch_is_logged(self):
        self.upload([self.order_record()], key='batch-log')

        batch = SyncBatch.objects.get(idempotency_key='batch-log')
        self.assertEqual(batch.user, self.executive)
        self.assertEqual(batch.device_id, 'pixel-7a')
        self.assertEqual(batch.status, BatchStatus.COMPLETED)
        self.assertEqual(batch.records_total, 1)
        self.assertEqual(batch.records_applied, 1)
        self.assertIsNotNone(batch.completed_at)
        self.assertIsNotNone(batch.duration_ms)

    def test_each_record_is_logged(self):
        self.upload([self.order_record(), self.customer_record()])

        records = SyncRecord.objects.all()
        self.assertEqual(records.count(), 2)
        self.assertEqual(
            set(records.values_list('entity_type', flat=True)),
            {'orders', 'customers'},
        )

    def test_an_empty_batch_is_refused(self):
        response = self.upload([])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_batch_bigger_than_the_limit_is_refused(self):
        records = [self.order_record(local_id=f'l-{i}') for i in range(201)]

        response = self.upload(records)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('records', response.data)

    def test_the_same_row_twice_in_one_batch_is_refused(self):
        response = self.upload([self.order_record(), self.order_record()])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('twice', str(response.data))


class IdempotencyTests(SyncTestCase):
    def test_the_same_key_replays_without_writing_again(self):
        first = self.upload([self.order_record()], key='same-key')
        second = self.upload([self.order_record()], key='same-key')

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data['replayed'])
        self.assertEqual(Order.objects.count(), 1)

    def test_a_replay_returns_the_same_server_ids(self):
        first = self.upload([self.order_record()], key='same-key')
        second = self.upload([self.order_record()], key='same-key')

        self.assertEqual(
            first.data['results'][0]['server_id'],
            second.data['results'][0]['server_id'],
        )

    def test_a_replay_does_not_add_a_second_batch(self):
        self.upload([self.order_record()], key='same-key')
        self.upload([self.order_record()], key='same-key')

        self.assertEqual(SyncBatch.objects.count(), 1)
        self.assertEqual(SyncRecord.objects.count(), 1)

    def test_the_same_row_under_a_new_key_is_recognised(self):
        """The device regenerated its batch key. The row must not double."""
        first = self.upload([self.order_record()], key='key-1')
        second = self.upload([self.order_record()], key='key-2')

        self.assertEqual(Order.objects.count(), 1)
        result = second.data['results'][0]
        self.assertEqual(result['status'], RecordStatus.DUPLICATE)
        self.assertEqual(result['server_id'], first.data['results'][0]['server_id'])

    def test_another_user_may_reuse_a_key(self):
        self.upload([self.order_record()], key='shared-key')

        self.client.force_authenticate(self.colleague)
        response = self.upload(
            [self.order_record(local_id='their-order')], key='shared-key'
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data['replayed'])
        self.assertEqual(Order.objects.count(), 2)

    def test_a_key_is_generated_when_none_is_sent(self):
        response = self.client.post(
            self.upload_url,
            {'records': [self.order_record()]},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['idempotency_key'])

    def test_a_module_level_sync_id_replay_is_reported_as_duplicate(self):
        """Layer 3: the beat module recognises its own sync_id."""
        beat = Beat.objects.create(
            code='DEL-N-01', name='Karol Bagh North', area='Karol Bagh',
            city='New Delhi', assigned_user=self.executive, weekdays=[1, 4],
        )
        # The beat module refuses to schedule a route with nothing on it.
        BeatOutlet.objects.create(
            beat=beat, customer_ref=str(self.customer.pk),
            customer_name=self.customer.name, sequence=1,
        )
        shared_sync_id = '019fd0f4-0000-0000-0000-0000000000aa'
        day = timezone.localdate().isoformat()

        def plan_record(local_id):
            return {
                'entity_type': 'beat_plans',
                'local_id': local_id,
                'operation': 'create',
                'payload': {
                    'beat': str(beat.pk),
                    'date': day,
                    'sync_id': shared_sync_id,
                },
            }

        self.upload([plan_record('plan-a')], key='k1')
        second = self.upload([plan_record('plan-b')], key='k2')

        # Different local_id, so layers 1 and 2 both miss it — the beat
        # module's own sync_id is what caught this one.
        self.assertEqual(BeatPlan.objects.count(), 1)
        self.assertEqual(
            second.data['results'][0]['status'], RecordStatus.DUPLICATE
        )


class PartialFailureTests(SyncTestCase):
    def test_one_bad_record_does_not_reject_the_others(self):
        records = [
            self.order_record(local_id='good-1'),
            {
                'entity_type': 'orders',
                'local_id': 'bad-1',
                'operation': 'create',
                'payload': {'customer': str(self.customer.pk), 'items': []},
            },
            self.customer_record(local_id='good-2'),
        ]

        response = self.upload(records)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['summary']['applied'], 2)
        self.assertEqual(response.data['summary']['failed'], 1)
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Customer.objects.count(), 2)

    def test_a_failed_record_leaves_nothing_behind(self):
        """The order's own transaction rolls back inside the batch."""
        records = [
            {
                'entity_type': 'orders',
                'local_id': 'bad-1',
                'operation': 'create',
                'payload': {
                    'customer': str(self.customer.pk),
                    'items': [
                        {'product': str(self.cement.pk), 'quantity': 5},
                        {'product': '019fd0f4-0000-0000-0000-000000000000',
                         'quantity': 1},
                    ],
                },
            }
        ]

        response = self.upload(records)

        self.assertEqual(response.data['summary']['failed'], 1)
        self.assertEqual(Order.objects.count(), 0)
        from orders.models import OrderItem

        self.assertEqual(OrderItem.objects.count(), 0)

    def test_the_failure_carries_the_owning_modules_words(self):
        records = [
            {
                'entity_type': 'orders',
                'local_id': 'bad-1',
                'operation': 'create',
                'payload': {'customer': str(self.customer.pk), 'items': []},
            }
        ]

        response = self.upload(records)

        detail = response.data['results'][0]['detail']
        self.assertIn('at least one product', str(detail))

    def test_a_batch_where_everything_fails_is_marked_failed(self):
        records = [
            {
                'entity_type': 'orders',
                'local_id': 'bad-1',
                'operation': 'create',
                'payload': {'customer': str(self.customer.pk), 'items': []},
            }
        ]

        self.upload(records, key='all-bad')

        self.assertEqual(
            SyncBatch.objects.get(idempotency_key='all-bad').status,
            BatchStatus.FAILED,
        )

    def test_a_retry_after_a_partial_failure_applies_only_what_failed(self):
        records = [
            self.order_record(local_id='good-1'),
            {
                'entity_type': 'orders',
                'local_id': 'bad-1',
                'operation': 'create',
                'payload': {'customer': str(self.customer.pk), 'items': []},
            },
        ]
        self.upload(records, key='first-try')

        # The device fixes the bad row and sends the queue again under a new
        # key. The good row must not be applied twice.
        records[1]['payload']['items'] = [
            {'product': str(self.cement.pk), 'quantity': 3}
        ]
        response = self.upload(records, key='second-try')

        statuses = {r['local_id']: r['status'] for r in response.data['results']}
        self.assertEqual(statuses['good-1'], RecordStatus.DUPLICATE)
        self.assertEqual(statuses['bad-1'], RecordStatus.APPLIED)
        self.assertEqual(Order.objects.count(), 2)


class ValidationTests(SyncTestCase):
    def test_an_unknown_entity_is_refused(self):
        response = self.upload(
            [{'entity_type': 'spaceships', 'local_id': 'x', 'operation': 'create',
              'payload': {}}]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unknown_operation_is_refused(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'x', 'operation': 'explode',
              'payload': {}}]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_update_without_a_server_id_is_refused(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'x', 'operation': 'update',
              'payload': {}}]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('server_id', str(response.data))

    def test_an_entity_that_cannot_be_created_from_a_device_says_so(self):
        plan = self.make_plan()
        visit = plan.visits.first()

        response = self.upload(
            [{'entity_type': 'beat_visits', 'local_id': 'v1',
              'operation': 'create', 'server_id': str(visit.pk), 'payload': {}}]
        )

        result = response.data['results'][0]
        self.assertEqual(result['status'], RecordStatus.FAILED)
        self.assertIn('cannot be created', str(result['detail']))

    def test_an_unknown_action_lists_the_known_ones(self):
        order = self.make_order()

        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o1', 'operation': 'update',
              'server_id': str(order.pk), 'payload': {'action': 'teleport'}}]
        )

        detail = str(response.data['results'][0]['detail'])
        self.assertIn('teleport', detail)
        self.assertIn('submit', detail)

    def make_order(self):
        response = self.upload([self.order_record()], key=f'mk-{timezone.now()}')
        return Order.objects.get(pk=response.data['results'][0]['server_id'])

    def make_plan(self):
        beat = Beat.objects.create(
            code='DEL-N-02', name='Route', area='A', city='B',
            assigned_user=self.executive, weekdays=[1],
        )
        plan = BeatPlan.objects.create(
            beat=beat, user=self.executive, date=timezone.localdate(),
            planned_outlet_count=1,
        )
        BeatPlanVisit.objects.create(
            plan=plan, customer_ref=str(self.customer.pk),
            customer_name=self.customer.name, sequence=1,
        )
        return plan


class ActionTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        response = self.upload([self.order_record()], key='make-order')
        self.order = Order.objects.get(
            pk=response.data['results'][0]['server_id']
        )

    def test_an_offline_submit_is_applied(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-submit',
              'operation': 'update', 'server_id': str(self.order.pk),
              'payload': {'action': 'submit'}}],
            key='submit-batch',
        )

        self.order.refresh_from_db()
        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertEqual(self.order.status, OrderStatus.SUBMITTED)

    def test_an_offline_cancel_carries_its_reason(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-cancel',
              'operation': 'update', 'server_id': str(self.order.pk),
              'payload': {'action': 'cancel', 'reason': 'Customer withdrew'}}],
            key='cancel-batch',
        )

        self.order.refresh_from_db()
        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertEqual(self.order.status, OrderStatus.CANCELLED)
        self.assertEqual(self.order.cancellation_reason, 'Customer withdrew')

    def test_a_field_edit_is_applied(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-edit',
              'operation': 'update', 'server_id': str(self.order.pk),
              'payload': {'remarks': 'Edited offline'}}],
            key='edit-batch',
        )

        self.order.refresh_from_db()
        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertEqual(self.order.remarks, 'Edited offline')

    def test_a_delete_is_applied_to_a_draft(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-del',
              'operation': 'delete', 'server_id': str(self.order.pk),
              'payload': {}}],
            key='delete-batch',
        )

        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertFalse(Order.objects.filter(pk=self.order.pk).exists())

    def test_a_beat_run_syncs_start_and_stops(self):
        beat = Beat.objects.create(
            code='DEL-N-03', name='Route', area='A', city='B',
            assigned_user=self.executive, weekdays=[1],
        )
        plan = BeatPlan.objects.create(
            beat=beat, user=self.executive, date=timezone.localdate(),
            planned_outlet_count=1,
        )
        stop = BeatPlanVisit.objects.create(
            plan=plan, customer_ref=str(self.customer.pk),
            customer_name=self.customer.name, sequence=1,
        )

        response = self.upload(
            [
                {'entity_type': 'beat_plans', 'local_id': 'p1',
                 'operation': 'update', 'server_id': str(plan.pk),
                 'payload': {'action': 'start'}},
                {'entity_type': 'beat_visits', 'local_id': 's1',
                 'operation': 'update', 'server_id': str(stop.pk),
                 'payload': {'action': 'visit', 'plan_id': str(plan.pk)}},
            ],
            key='beat-batch',
        )

        plan.refresh_from_db()
        stop.refresh_from_db()
        self.assertEqual(response.data['summary']['applied'], 2)
        self.assertEqual(stop.status, StopStatus.VISITED)

        # Not `in_progress`: the beat module closes a run once every stop is
        # resolved, and this plan had one stop. That rule was not reimplemented
        # here — it fired because the upload went through the real endpoint,
        # which is the whole point of the dispatch design.
        self.assertEqual(plan.status, BeatPlanStatus.COMPLETED)
        self.assertIsNotNone(plan.closed_at)


class ConflictTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        response = self.upload([self.order_record()], key='make-order')
        self.order = Order.objects.get(
            pk=response.data['results'][0]['server_id']
        )

    def test_a_stale_update_is_refused(self):
        stale = self.order.updated_at - timedelta(minutes=5)

        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-stale',
              'operation': 'update', 'server_id': str(self.order.pk),
              'sync_version': stale.isoformat(),
              'payload': {'remarks': 'Written from a stale copy'}}],
            key='stale-batch',
        )

        result = response.data['results'][0]
        self.order.refresh_from_db()
        self.assertEqual(result['status'], RecordStatus.CONFLICT)
        self.assertEqual(result['http_status'], status.HTTP_409_CONFLICT)
        self.assertNotEqual(self.order.remarks, 'Written from a stale copy')

    def test_a_conflict_returns_the_current_server_state(self):
        stale = self.order.updated_at - timedelta(minutes=5)

        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-stale',
              'operation': 'update', 'server_id': str(self.order.pk),
              'sync_version': stale.isoformat(), 'payload': {'remarks': 'x'}}],
            key='stale-batch',
        )

        result = response.data['results'][0]
        self.assertIn('server_version', result['detail'])
        self.assertIn('your_version', result['detail'])
        self.assertEqual(result['data']['id'], str(self.order.pk))

    def test_a_current_version_is_accepted(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-fresh',
              'operation': 'update', 'server_id': str(self.order.pk),
              'sync_version': self.order.updated_at.isoformat(),
              'payload': {'remarks': 'Written from a current copy'}}],
            key='fresh-batch',
        )

        self.order.refresh_from_db()
        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertEqual(self.order.remarks, 'Written from a current copy')

    def test_no_version_claimed_means_last_write_wins(self):
        response = self.upload(
            [{'entity_type': 'orders', 'local_id': 'o-nover',
              'operation': 'update', 'server_id': str(self.order.pk),
              'payload': {'remarks': 'No claim made'}}],
            key='nover-batch',
        )

        self.order.refresh_from_db()
        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertEqual(self.order.remarks, 'No claim made')

    def test_a_business_rule_conflict_is_reported_as_a_conflict(self):
        """409 from the owning module, not from the version check."""
        from attendance.models import Attendance

        Attendance.objects.create(
            user=self.executive, day=timezone.localdate(),
            punch_in_at=timezone.now(),
            punch_in_latitude=Decimal('28.612900'),
            punch_in_longitude=Decimal('77.229500'),
        )

        with override_settings(ATTENDANCE_SELFIE_REQUIRED=False):
            response = self.upload(
                [{'entity_type': 'attendance', 'local_id': 'a1',
                  'operation': 'create',
                  'payload': {'latitude': '28.612900', 'longitude': '77.229500',
                              'accuracy': 8.0}}],
                key='dup-punch',
            )

        result = response.data['results'][0]
        self.assertEqual(result['status'], RecordStatus.CONFLICT)
        self.assertEqual(result['http_status'], status.HTTP_409_CONFLICT)


class PermissionTests(SyncTestCase):
    def test_upload_needs_a_token(self):
        self.client.force_authenticate(None)

        response = self.upload([self.order_record()])

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_download_needs_a_token(self):
        self.client.force_authenticate(None)

        self.assertEqual(
            self.client.get(self.download_url).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_status_needs_a_token(self):
        self.client.force_authenticate(None)

        self.assertEqual(
            self.client.get(self.status_url).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_a_record_the_user_may_not_write_is_refused_per_record(self):
        """The owning endpoint's permission still applies inside a batch."""
        self.client.force_authenticate(self.observer)

        response = self.upload([self.order_record()], key='no-perm')

        result = response.data['results'][0]
        self.assertEqual(result['status'], RecordStatus.FAILED)
        self.assertEqual(result['http_status'], status.HTTP_403_FORBIDDEN)
        self.assertEqual(Order.objects.count(), 0)

    def test_a_batch_cannot_write_to_another_users_record(self):
        response = self.upload([self.order_record()], key='mine')
        order_id = response.data['results'][0]['server_id']

        self.client.force_authenticate(self.colleague)
        theirs = self.upload(
            [{'entity_type': 'orders', 'local_id': 'steal',
              'operation': 'update', 'server_id': order_id,
              'payload': {'remarks': 'Mine now'}}],
            key='steal',
        )

        result = theirs.data['results'][0]
        self.assertEqual(result['status'], RecordStatus.FAILED)
        self.assertEqual(result['http_status'], status.HTTP_404_NOT_FOUND)


class DownloadTests(SyncTestCase):
    def setUp(self):
        super().setUp()
        self.upload([self.order_record()], key='seed')

    def test_a_first_sync_returns_everything(self):
        response = self.client.get(self.download_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['entities']['orders']['count'], 1)
        self.assertEqual(response.data['entities']['customers']['count'], 1)
        self.assertTrue(response.data['server_time'])

    def test_an_incremental_sync_returns_only_what_changed(self):
        cutoff = timezone.now()

        response = self.client.get(
            self.download_url, {'last_sync_at': cutoff.isoformat()}
        )

        self.assertEqual(response.data['records_returned'], 0)

        self.upload([self.order_record(local_id='later')], key='later')
        response = self.client.get(
            self.download_url, {'last_sync_at': cutoff.isoformat()}
        )

        self.assertEqual(response.data['entities']['orders']['count'], 1)

    def test_a_device_only_gets_its_own_records(self):
        self.client.force_authenticate(self.colleague)
        self.upload([self.order_record(local_id='theirs')], key='theirs')

        response = self.client.get(self.download_url)

        # Their own order, not the executive's.
        self.assertEqual(response.data['entities']['orders']['count'], 1)
        ids = {r['id'] for r in response.data['entities']['orders']['records']}
        self.assertEqual(
            ids, {str(Order.objects.get(employee=self.colleague).pk)}
        )

    def test_the_customer_book_is_shared(self):
        self.client.force_authenticate(self.colleague)

        response = self.client.get(self.download_url)

        # Master data: everybody sells to the same customers.
        self.assertEqual(response.data['entities']['customers']['count'], 1)

    def test_a_subset_of_entities_can_be_asked_for(self):
        response = self.client.get(self.download_url, {'entities': 'orders'})

        self.assertEqual(list(response.data['entities']), ['orders'])

    def test_an_unknown_entity_is_refused(self):
        response = self.client.get(self.download_url, {'entities': 'spaceships'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_malformed_timestamp_is_refused(self):
        response = self.client.get(
            self.download_url, {'last_sync_at': 'last tuesday'}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_page_limit_reports_more_to_come(self):
        for index in range(4):
            self.upload(
                [self.order_record(local_id=f'extra-{index}')], key=f'k{index}'
            )

        response = self.client.get(
            self.download_url, {'entities': 'orders', 'limit': 2}
        )

        block = response.data['entities']['orders']
        self.assertEqual(block['count'], 2)
        self.assertTrue(block['has_more'])
        self.assertIsNotNone(block['cursor'])

    def test_the_cursor_resumes_without_skipping_or_repeating(self):
        for index in range(4):
            self.upload(
                [self.order_record(local_id=f'extra-{index}')], key=f'k{index}'
            )

        first = self.client.get(
            self.download_url, {'entities': 'orders', 'limit': 2}
        )
        second = self.client.get(
            self.download_url,
            {
                'entities': 'orders',
                'limit': 2,
                'last_sync_at': first.data['entities']['orders']['cursor'],
            },
        )

        first_ids = {r['id'] for r in first.data['entities']['orders']['records']}
        second_ids = {r['id'] for r in second.data['entities']['orders']['records']}
        self.assertEqual(first_ids & second_ids, set())

    def test_an_empty_download_is_not_an_error(self):
        self.client.force_authenticate(self.observer)

        response = self.client.get(self.download_url, {'entities': 'orders'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['records_returned'], 0)

    def test_a_download_is_logged(self):
        self.client.get(self.download_url, {'device_id': 'pixel-7a'})

        log = SyncDownloadLog.objects.latest('created_at')
        self.assertEqual(log.user, self.executive)
        self.assertEqual(log.device_id, 'pixel-7a')
        self.assertIsNotNone(log.duration_ms)
        self.assertGreater(log.records_returned, 0)


class StatusTests(SyncTestCase):
    def test_the_status_lists_what_can_be_synced(self):
        response = self.client.get(self.status_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['sync_version'], 1)
        self.assertIn('orders', response.data['supported_entities'])
        self.assertIn('attendance', response.data['supported_entities'])
        self.assertTrue(response.data['server_time'])

    def test_the_status_describes_each_entitys_operations(self):
        response = self.client.get(self.status_url)

        orders = next(
            e for e in response.data['entities'] if e['entity_type'] == 'orders'
        )
        self.assertEqual(orders['operations'], ['create', 'delete', 'update'])
        self.assertEqual(orders['actions'], ['cancel', 'submit'])

    def test_the_status_reports_the_last_upload(self):
        self.upload([self.order_record()], key='last')

        response = self.client.get(self.status_url)

        self.assertIsNotNone(response.data['last_upload'])
        self.assertEqual(response.data['last_upload']['applied'], 1)

    def test_a_device_that_has_never_synced_has_no_last_upload(self):
        response = self.client.get(self.status_url)

        self.assertIsNone(response.data['last_upload'])


class PerformanceTests(SyncTestCase):
    def test_a_large_batch_completes(self):
        """Fifty customers in one upload, each through the real endpoint."""
        records = [
            self.customer_record(local_id=f'c-{i}', phone=f'98111{i:05d}')
            for i in range(50)
        ]

        response = self.upload(records, key='large')

        self.assertEqual(response.data['summary']['applied'], 50)
        self.assertEqual(Customer.objects.count(), 51)

    def test_the_download_does_not_grow_a_query_per_record(self):
        """The N+1 guard: ten orders with lines cost the same as one."""
        self.upload([self.order_record()], key='one')
        self.client.get(self.download_url, {'entities': 'orders'})

        with self.assertNumQueries(4):
            self.client.get(self.download_url, {'entities': 'orders'})

        for index in range(10):
            self.upload(
                [self.order_record(local_id=f'more-{index}')], key=f'more-{index}'
            )

        with self.assertNumQueries(4):
            self.client.get(self.download_url, {'entities': 'orders'})


class PhotoUploadTests(SyncTestCase):
    """A punch made with no signal arrives with its selfie attached.

    This is the case the module existed for and could not do: a selfie is
    mandatory on attendance, `/sync/upload/` took JSON only, and a queued
    punch would therefore have been refused on every replay for ever.
    """

    def selfie(self, name='selfie.jpg'):
        buffer = io.BytesIO()
        Image.new('RGB', (4, 4), color='white').save(buffer, format='JPEG')
        buffer.seek(0)
        return SimpleUploadedFile(name, buffer.read(), content_type='image/jpeg')

    def punch_record(self, local_id='local-punch-1'):
        return {
            'entity_type': 'attendance',
            'local_id': local_id,
            'operation': 'create',
            'device_timestamp': timezone.now().isoformat(),
            'payload': {
                'latitude': '28.6139',
                'longitude': '77.2090',
                'accuracy': '8',
                'captured_at': timezone.now().isoformat(),
            },
        }

    def upload_multipart(self, records, key='photo-batch-1', **files):
        return self.client.post(
            self.upload_url,
            {
                'idempotency_key': key,
                'device_id': 'pixel-7a',
                'records': json.dumps(records),
                **files,
            },
            format='multipart',
        )

    def test_a_queued_punch_arrives_with_its_selfie(self):
        response = self.upload_multipart(
            [self.punch_record()],
            **{'file.0.selfie': self.selfie()},
        )

        self.assertEqual(response.status_code, 201)
        result = response.data['results'][0]
        self.assertEqual(result['status'], 'applied', result.get('detail'))
        self.assertTrue(result['server_id'])

        punch = Attendance.objects.get(pk=result['server_id'])
        self.assertTrue(punch.punch_in_selfie)
        # The image is really on disk, not an empty file with a name.
        self.assertGreater(punch.punch_in_selfie.size, 0)

    def test_the_punch_keeps_the_moment_it_happened(self):
        happened = timezone.now() - timedelta(hours=3)
        record = self.punch_record()
        record['payload']['captured_at'] = happened.isoformat()

        response = self.upload_multipart(
            [record], **{'file.0.selfie': self.selfie()}
        )

        punch = Attendance.objects.get(
            pk=response.data['results'][0]['server_id']
        )
        # A punch recorded in a basement at 09:04 is a 09:04 punch.
        self.assertLess(abs((punch.punch_in_at - happened).total_seconds()), 5)

    def test_a_punch_without_its_selfie_is_refused_not_written(self):
        response = self.upload_multipart([self.punch_record()])

        result = response.data['results'][0]
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['http_status'], 400)
        self.assertFalse(Attendance.objects.exists())

    def test_an_image_that_is_not_an_image_is_refused(self):
        # The module's own Pillow check runs, exactly as it does online — a
        # PHP script named .jpg does not become a selfie.
        payload = SimpleUploadedFile(
            'selfie.jpg',
            b'<?php system($_GET["c"]); ?>',
            content_type='image/jpeg',
        )

        response = self.upload_multipart(
            [self.punch_record()], **{'file.0.selfie': payload}
        )

        result = response.data['results'][0]
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(Attendance.objects.exists())

    def test_replaying_the_batch_does_not_write_a_second_punch(self):
        first = self.upload_multipart(
            [self.punch_record()], **{'file.0.selfie': self.selfie()}
        )
        self.assertEqual(first.status_code, 201)

        second = self.upload_multipart(
            [self.punch_record()], **{'file.0.selfie': self.selfie()}
        )

        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data['replayed'])
        self.assertEqual(Attendance.objects.count(), 1)

    def test_a_photo_record_and_a_plain_one_travel_together(self):
        response = self.upload_multipart(
            [self.punch_record(), self.customer_record()],
            **{'file.0.selfie': self.selfie()},
        )

        statuses = [row['status'] for row in response.data['results']]
        self.assertEqual(statuses, ['applied', 'applied'])
        self.assertTrue(Attendance.objects.exists())
        self.assertTrue(Customer.objects.filter(phone='9822233344').exists())

    def test_a_file_naming_a_record_outside_the_batch_is_refused(self):
        response = self.upload_multipart(
            [self.punch_record()],
            **{'file.7.selfie': self.selfie()},
        )

        # Silence would mean a device believed it sent a photo and got a punch
        # without one.
        self.assertEqual(response.status_code, 400)
        self.assertIn('files', response.data)

    def test_a_malformed_attachment_name_is_refused(self):
        response = self.upload_multipart(
            [self.punch_record()],
            **{'selfie': self.selfie()},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('files', response.data)

    def test_records_must_still_be_a_json_array(self):
        response = self.client.post(
            self.upload_url,
            {'idempotency_key': 'bad', 'records': 'not json'},
            format='multipart',
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('records', response.data)

    def test_a_json_batch_still_works_unchanged(self):
        # Backward compatibility: nothing about the JSON path moved.
        response = self.upload([self.order_record()], key='json-still-fine')

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['results'][0]['status'], 'applied')


class ModuleRouteTests(SyncTestCase):
    """The two routes Phase 13B added, exercised through the real endpoints.

    Everything else the four modules queue was already routed. These two were
    not, so an offline customer edit and an offline site photo had nowhere to
    go: the record would have been refused as an unsupported operation, over
    and over, until somebody threw the queue away.
    """

    def site(self):
        return Site.objects.create(
            name='Green Valley Phase 2', code='SITE-01',
            customer_ref=str(self.customer.pk), customer_name=self.customer.name,
            city='Gurgaon', pincode='122003',
        )

    def photo(self, name='site.jpg'):
        buffer = io.BytesIO()
        Image.new('RGB', (4, 4), color='white').save(buffer, format='JPEG')
        buffer.seek(0)
        return SimpleUploadedFile(name, buffer.read(), content_type='image/jpeg')

    def test_a_customer_edited_offline_is_patched_not_recreated(self):
        response = self.upload(
            [{'entity_type': 'customers', 'local_id': 'c-edit',
              'operation': 'update', 'server_id': str(self.customer.pk),
              'payload': {'phone': '9800000000'}}],
            key='customer-edit',
        )

        self.customer.refresh_from_db()
        self.assertEqual(response.data['results'][0]['status'], RecordStatus.APPLIED)
        self.assertEqual(self.customer.phone, '9800000000')
        # A PATCH of one field, so the rest of the record is untouched — which
        # is the point of sending only what changed.
        self.assertEqual(self.customer.contact_person, 'Ramesh Gupta')
        self.assertEqual(Customer.objects.count(), 1)

    def test_a_site_visit_and_its_photo_sync_as_one_chain(self):
        site = self.site()

        opened = self.upload(
            [{'entity_type': 'site_visits', 'local_id': 'v1',
              'operation': 'create',
              'payload': {'site': str(site.pk), 'purpose': 'follow_up',
                          'latitude': '28.6139', 'longitude': '77.2090',
                          'captured_at': timezone.now().isoformat()}}],
            key='visit-open',
        )
        visit_id = opened.data['results'][0]['server_id']
        self.assertTrue(visit_id)

        # The photo goes up in its own batch, addressed to the visit the
        # server just gave back — which is exactly what the device does once
        # the check-in it depended on has landed.
        response = self.client.post(
            self.upload_url,
            {
                'idempotency_key': 'visit-photo',
                'device_id': 'pixel-7a',
                'records': json.dumps([
                    {'entity_type': 'site_visits', 'local_id': 'v1-img',
                     'operation': 'update', 'server_id': visit_id,
                     'payload': {'action': 'add_image', 'tag': 'site_front',
                                 'latitude': '28.6139',
                                 'longitude': '77.2090'}},
                ]),
                'file.0.image': self.photo(),
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, 201)
        result = response.data['results'][0]
        self.assertEqual(result['status'], RecordStatus.APPLIED, result.get('detail'))

        visit = SiteVisit.objects.get(pk=visit_id)
        image = visit.images.get()
        self.assertEqual(image.tag, 'site_front')
        self.assertGreater(image.image.size, 0)

    def test_a_photo_for_a_closed_visit_is_refused_not_written(self):
        site = self.site()
        visit = SiteVisit.objects.create(
            site=site, user=self.executive, purpose='follow_up',
            check_in_at=timezone.now(), check_out_at=timezone.now(),
            check_in_latitude=Decimal('28.6139'),
            check_in_longitude=Decimal('77.2090'),
            status='completed',
        )

        response = self.client.post(
            self.upload_url,
            {
                'idempotency_key': 'late-photo',
                'device_id': 'pixel-7a',
                'records': json.dumps([
                    {'entity_type': 'site_visits', 'local_id': 'late-img',
                     'operation': 'update', 'server_id': str(visit.pk),
                     'payload': {'action': 'add_image', 'tag': 'site_front',
                                 'latitude': '28.6139',
                                 'longitude': '77.2090'}},
                ]),
                'file.0.image': self.photo(),
            },
            format='multipart',
        )

        # The module's own rule — a photo added after the fact is not evidence
        # of what was there — applies to a replayed record too, because the
        # record went through the module's endpoint.
        self.assertNotEqual(
            response.data['results'][0]['status'], RecordStatus.APPLIED
        )
        self.assertEqual(visit.images.count(), 0)
