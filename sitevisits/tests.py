"""End-to-end tests for the site visit endpoints.

Run against a throwaway `test_sfm_db`; uploaded photos go to a temporary
MEDIA_ROOT that is removed afterwards.
"""

import shutil
import tempfile
import uuid
from datetime import timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from .models import Site, SiteVisit, VisitStatus, haversine_metres

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'
MEDIA_ROOT = tempfile.mkdtemp()

SITE_LAT, SITE_LNG = 28.612900, 77.229500
NEARBY_LAT, NEARBY_LNG = 28.613500, 77.230100


def photo(name='site.jpg'):
    """A real JPEG — ImageField refuses anything that will not decode."""
    buffer = BytesIO()
    Image.new('RGB', (1, 1), color='white').save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class SiteVisitTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Field Executive', code='field_exec')
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='log_site_visits'
            )
        )
        cls.user = User.objects.create_user(
            'sfm-0002',
            'Alex Mercer',
            email='alex@corp.com',
            mobile='+919876543210',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
        )
        cls.colleague = User.objects.create_user(
            'sfm-0003',
            'Sneha Kulkarni',
            email='sneha@corp.com',
            mobile='+919876500044',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
        )
        # Same role withheld, to prove the permission class bites.
        cls.outsider = User.objects.create_user(
            'sfm-0004',
            'Vikram Rao',
            email='vikram@corp.com',
            mobile='+919876500055',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )

        cls.site = Site.objects.create(
            name='Green Valley Apartments',
            code='SITE-01',
            customer_ref='cus_1',
            customer_name='Shree Balaji Traders',
            address='Plot 14, Sector 62',
            city='Noida',
            latitude=SITE_LAT,
            longitude=SITE_LNG,
        )
        cls.unplotted = Site.objects.create(
            name='Riverfront Villas',
            code='SITE-02',
            customer_ref='cus_2',
            customer_name='Verma Hardware',
            city='New Delhi',
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.authenticate(self.user)

    # ------------------------------------------------------------------ helpers

    def url(self, name, **kwargs):
        return reverse(f'sitevisits:{name}', kwargs={'version': 'v1', **kwargs})

    def authenticate(self, user):
        token = self.client.post(
            reverse('accounts:login', kwargs={'version': 'v1'}),
            {'identifier': user.employee_code, 'password': PASSWORD},
            format='json',
        ).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def check_in(self, **overrides):
        payload = {
            'site': str(self.site.id),
            'purpose': 'follow_up',
            'latitude': str(NEARBY_LAT),
            'longitude': str(NEARBY_LNG),
            'accuracy': 12,
            'address': 'Sector 62, Noida',
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(self.url('check-in'), payload, format='json')

    def check_out(self, visit_id, **overrides):
        payload = {
            'latitude': str(NEARBY_LAT),
            'longitude': str(NEARBY_LNG),
            'accuracy': 9,
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(
            self.url('check-out', pk=visit_id), payload, format='json'
        )

    def open_visit(self):
        response = self.check_in()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data['id']

    # ------------------------------------------------------------------- sites

    def test_the_site_list_shows_active_sites_in_the_clients_shape(self):
        response = self.client.get(self.url('site-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        site = next(s for s in response.data['results'] if s['code'] == 'SITE-01')
        self.assertEqual(site['customer_id'], 'cus_1')
        self.assertEqual(site['location']['lat'], SITE_LAT)
        self.assertEqual(site['stage'], 'foundation')

    def test_an_unplotted_site_reports_no_location_rather_than_zeroes(self):
        response = self.client.get(self.url('site-list'))
        site = next(s for s in response.data['results'] if s['code'] == 'SITE-02')
        self.assertIsNone(site['location'])

    # ---------------------------------------------------------------- check-in

    def test_checking_in_opens_a_visit_and_measures_the_distance(self):
        response = self.check_in()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        body = response.data
        self.assertTrue(body['is_open'])
        self.assertEqual(body['status'], VisitStatus.IN_PROGRESS)
        self.assertEqual(body['site_name'], 'Green Valley Apartments')
        self.assertEqual(body['customer_name'], 'Shree Balaji Traders')
        self.assertEqual(body['check_in_location']['lat'], NEARBY_LAT)
        # Roughly 90 metres from the plotted pin.
        self.assertLess(body['check_in_distance_meters'], 150)

    def test_a_site_with_no_pin_records_no_distance(self):
        response = self.check_in(site=str(self.unplotted.id))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data['check_in_distance_meters'])

    def test_a_far_away_check_in_is_recorded_not_refused(self):
        # A pin is often approximate; refusing the visit would help nobody.
        response = self.check_in(latitude='28.90', longitude='77.60')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertGreater(response.data['check_in_distance_meters'], 10000)

    def test_a_second_visit_while_one_is_open_is_refused(self):
        self.open_visit()
        response = self.check_in()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('already checked in', response.data['detail'])
        self.assertEqual(SiteVisit.objects.count(), 1)

    def test_check_in_requires_the_log_site_visits_permission(self):
        self.authenticate(self.outsider)
        self.assertEqual(self.check_in().status_code, status.HTTP_403_FORBIDDEN)

    def test_check_in_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(self.check_in().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_an_inactive_site_cannot_be_visited(self):
        self.unplotted.is_active = False
        self.unplotted.save(update_fields=['is_active'])
        response = self.check_in(site=str(self.unplotted.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.unplotted.is_active = True
        self.unplotted.save(update_fields=['is_active'])

    # ---------------------------------------------------------- GPS validation

    def test_impossible_coordinates_are_refused(self):
        response = self.check_in(latitude='95.0')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('latitude', response.data)

    def test_a_null_island_fix_is_refused(self):
        response = self.check_in(latitude='0.0', longitude='0.0')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_hopeless_accuracy_is_refused(self):
        response = self.check_in(accuracy=450)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('accuracy', response.data)

    def test_a_visit_stamped_in_the_future_is_refused(self):
        ahead = (timezone.now() + timedelta(hours=2)).isoformat()
        response = self.check_in(captured_at=ahead)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_visit_older_than_the_sync_window_is_refused(self):
        stale = (timezone.now() - timedelta(days=30)).isoformat()
        response = self.check_in(captured_at=stale)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_haversine_matches_a_known_distance(self):
        # India Gate to Red Fort is about 5 km.
        metres = haversine_metres(28.612900, 77.229500, 28.656200, 77.241000)
        self.assertAlmostEqual(metres / 1000, 5.0, delta=0.8)

    # ------------------------------------------------------------- offline sync

    def test_replaying_a_sync_id_returns_the_original_visit(self):
        sync_id = str(uuid.uuid4())
        first = self.check_in(sync_id=sync_id)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        replay = self.check_in(sync_id=sync_id)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data['id'], first.data['id'])
        self.assertEqual(SiteVisit.objects.count(), 1)

    def test_a_visit_captured_offline_keeps_its_own_timestamp(self):
        captured = timezone.now() - timedelta(hours=5)
        self.check_in(captured_at=captured.isoformat())
        visit = SiteVisit.objects.get()
        self.assertAlmostEqual(
            visit.check_in_at, captured, delta=timedelta(seconds=1)
        )

    # --------------------------------------------------------------- check-out

    def test_checking_out_closes_the_visit_and_totals_the_minutes(self):
        started = timezone.now() - timedelta(minutes=45)
        visit_id = self.check_in(captured_at=started.isoformat()).data['id']

        response = self.check_out(
            visit_id,
            stage_observed='structure',
            competitor_brands=['Ambuja', 'ACC'],
            expected_order_value='125000.00',
            follow_up_date=str(timezone.localdate() + timedelta(days=7)),
            remarks='Slab work starts next week',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.data
        self.assertFalse(body['is_open'])
        self.assertEqual(body['status'], VisitStatus.COMPLETED)
        self.assertEqual(body['stage_observed'], 'structure')
        self.assertEqual(body['competitor_brands'], ['Ambuja', 'ACC'])
        self.assertEqual(body['remarks'], 'Slab work starts next week')
        # Server arithmetic, not a number the device sent.
        self.assertAlmostEqual(body['duration_minutes'], 45, delta=2)

    def test_checking_out_twice_is_refused(self):
        visit_id = self.open_visit()
        self.check_out(visit_id)
        self.assertEqual(
            self.check_out(visit_id).status_code, status.HTTP_409_CONFLICT
        )

    def test_check_out_before_check_in_is_refused(self):
        visit_id = self.open_visit()
        earlier = (timezone.now() - timedelta(hours=3)).isoformat()
        response = self.check_out(visit_id, captured_at=earlier)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_follow_up_in_the_past_is_refused(self):
        visit_id = self.open_visit()
        response = self.check_out(
            visit_id,
            follow_up_date=str(timezone.localdate() - timedelta(days=1)),
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('follow_up_date', response.data)

    def test_a_negative_order_value_is_refused(self):
        visit_id = self.open_visit()
        response = self.check_out(visit_id, expected_order_value='-1.00')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_another_users_visit_cannot_be_closed(self):
        visit_id = self.open_visit()
        self.authenticate(self.colleague)
        self.assertEqual(
            self.check_out(visit_id).status_code, status.HTTP_403_FORBIDDEN
        )

    # ------------------------------------------------------------------ photos

    def test_a_photo_can_be_added_to_an_open_visit(self):
        visit_id = self.open_visit()

        response = self.client.post(
            self.url('add-image', pk=visit_id),
            {
                'image': photo(),
                'tag': 'work_in_progress',
                'caption': 'Second floor slab',
                'latitude': str(NEARBY_LAT),
                'longitude': str(NEARBY_LNG),
            },
            format='multipart',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        image = response.data['images'][0]
        self.assertEqual(image['tag'], 'work_in_progress')
        self.assertEqual(image['caption'], 'Second floor slab')
        self.assertIn('.jpg', image['path'])
        self.assertEqual(image['location']['lat'], NEARBY_LAT)

    def test_something_that_is_not_an_image_is_refused(self):
        visit_id = self.open_visit()
        response = self.client.post(
            self.url('add-image', pk=visit_id),
            {'image': SimpleUploadedFile('note.txt', b'not a picture')},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_photo_cannot_be_added_after_the_visit_closes(self):
        visit_id = self.open_visit()
        self.check_out(visit_id)

        response = self.client.post(
            self.url('add-image', pk=visit_id),
            {'image': photo()},
            format='multipart',
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_photo_can_be_removed_while_the_visit_is_open(self):
        visit_id = self.open_visit()
        added = self.client.post(
            self.url('add-image', pk=visit_id),
            {'image': photo()},
            format='multipart',
        )
        image_id = added.data['images'][0]['id']

        response = self.client.delete(
            self.url('remove-image', pk=visit_id, image_pk=image_id)
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['images'], [])

    # ------------------------------------------------------------------ cancel

    def test_cancelling_needs_a_reason_and_keeps_the_record(self):
        visit_id = self.open_visit()

        self.assertEqual(
            self.client.post(
                self.url('cancel', pk=visit_id), {'reason': '  '}, format='json'
            ).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

        response = self.client.post(
            self.url('cancel', pk=visit_id),
            {'reason': 'Site was locked'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], VisitStatus.CANCELLED)
        self.assertEqual(response.data['remarks'], 'Site was locked')
        # Cancelled, not deleted.
        self.assertEqual(SiteVisit.objects.count(), 1)

    # ----------------------------------------------------------------- reading

    def test_the_open_visit_endpoint_answers_null_then_the_visit(self):
        self.assertIsNone(self.client.get(self.url('open')).data['visit'])

        self.open_visit()
        self.assertTrue(self.client.get(self.url('open')).data['visit']['is_open'])

    def test_history_shows_only_the_callers_own_visits(self):
        self.open_visit()

        self.authenticate(self.colleague)
        response = self.client.get(self.url('list'))
        self.assertEqual(response.data['count'], 0)

    def test_history_can_be_narrowed_to_follow_ups_that_are_due(self):
        visit_id = self.open_visit()
        self.check_out(visit_id, follow_up_date=str(timezone.localdate()))

        response = self.client.get(self.url('list'), {'follow_up_due': 'true'})
        self.assertEqual(response.data['count'], 1)

        # A follow-up still in the future is not due yet.
        SiteVisit.objects.update(
            follow_up_date=timezone.localdate() + timedelta(days=3)
        )
        response = self.client.get(self.url('list'), {'follow_up_due': 'true'})
        self.assertEqual(response.data['count'], 0)

    def test_another_users_visit_cannot_be_read(self):
        visit_id = self.open_visit()
        self.authenticate(self.colleague)
        response = self.client.get(self.url('detail', pk=visit_id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
