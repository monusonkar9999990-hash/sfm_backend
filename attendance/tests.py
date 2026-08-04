"""End-to-end tests for the attendance endpoints.

Run against a throwaway `test_sfm_db`; uploaded selfies go to a temporary
MEDIA_ROOT that is removed afterwards, so nothing is left behind on disk.
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
from .models import Attendance, GeoFence, haversine_metres

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'
MEDIA_ROOT = tempfile.mkdtemp()

# India Gate, and a point a few hundred metres away.
OFFICE_LAT, OFFICE_LNG = 28.612900, 77.229500
NEARBY_LAT, NEARBY_LNG = 28.613500, 77.230100
FAR_LAT, FAR_LNG = 28.700000, 77.400000


def selfie_file(name='selfie.jpg'):
    """A real 1x1 JPEG — ImageField refuses anything that will not decode."""
    buffer = BytesIO()
    Image.new('RGB', (1, 1), color='white').save(buffer, format='JPEG')
    buffer.seek(0)
    return SimpleUploadedFile(name, buffer.read(), content_type='image/jpeg')


@override_settings(MEDIA_ROOT=MEDIA_ROOT)
class AttendanceTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Field Executive', code='field_exec')
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='mark_attendance'
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
        # Same permissions withheld, to prove the permission class bites.
        cls.outsider = User.objects.create_user(
            'sfm-0003',
            'Sneha Kulkarni',
            email='sneha@corp.com',
            mobile='+919876500044',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )
        cls.fence = GeoFence.objects.create(
            name='Head Office',
            code='HO',
            latitude=OFFICE_LAT,
            longitude=OFFICE_LNG,
            radius_meters=300,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        cache.clear()
        self.authenticate(self.user)

    # ------------------------------------------------------------------ helpers

    def url(self, name):
        return reverse(f'attendance:{name}', kwargs={'version': 'v1'})

    def authenticate(self, user):
        token = self.client.post(
            reverse('accounts:login', kwargs={'version': 'v1'}),
            {'identifier': user.employee_code, 'password': PASSWORD},
            format='json',
        ).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def check_in(self, **overrides):
        payload = {
            'latitude': str(NEARBY_LAT),
            'longitude': str(NEARBY_LNG),
            'accuracy': '12',
            'address': 'India Gate, New Delhi',
            'selfie': selfie_file(),
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(self.url('check-in'), payload, format='multipart')

    def check_out(self, **overrides):
        payload = {
            'latitude': str(NEARBY_LAT),
            'longitude': str(NEARBY_LNG),
            'accuracy': '9',
            'selfie': selfie_file('out.jpg'),
        }
        payload.update(overrides)
        payload = {k: v for k, v in payload.items() if v is not None}
        return self.client.post(self.url('check-out'), payload, format='multipart')

    # ----------------------------------------------------------------- check-in

    def test_check_in_creates_the_days_record(self):
        response = self.check_in()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        body = response.data
        self.assertEqual(body['punch_in_location']['lat'], NEARBY_LAT)
        self.assertIsNotNone(body['punch_in_selfie'])
        self.assertTrue(body['is_open'])
        self.assertTrue(body['punch_in_within_fence'])
        self.assertEqual(Attendance.objects.count(), 1)

    def test_check_in_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(self.check_in().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_check_in_requires_the_mark_attendance_permission(self):
        self.authenticate(self.outsider)
        self.assertEqual(self.check_in().status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------- duplicate check-in guard

    def test_second_check_in_the_same_day_is_refused(self):
        self.assertEqual(self.check_in().status_code, status.HTTP_201_CREATED)

        response = self.check_in()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('already checked in', response.data['detail'])
        self.assertEqual(Attendance.objects.count(), 1)

    def test_duplicate_is_refused_even_after_checking_out(self):
        self.check_in()
        self.check_out()
        self.assertEqual(self.check_in().status_code, status.HTTP_409_CONFLICT)

    def test_the_database_itself_refuses_a_duplicate(self):
        # Not just the view: the constraint has to exist, or a race gets through.
        self.check_in()
        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Attendance.objects.create(
                    user=self.user,
                    day=timezone.localdate(),
                    punch_in_at=timezone.now(),
                    punch_in_latitude=NEARBY_LAT,
                    punch_in_longitude=NEARBY_LNG,
                )

    # -------------------------------------------------------------- offline sync

    def test_replaying_a_sync_id_returns_the_original_record(self):
        sync_id = str(uuid.uuid4())
        first = self.check_in(sync_id=sync_id)
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        replay = self.check_in(sync_id=sync_id)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data['id'], first.data['id'])
        self.assertEqual(Attendance.objects.count(), 1)

    def test_replaying_a_check_out_sync_id_is_idempotent(self):
        self.check_in()
        sync_id = str(uuid.uuid4())
        first = self.check_out(sync_id=sync_id)
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        replay = self.check_out(sync_id=sync_id)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data['punch_out_at'], first.data['punch_out_at'])

    def test_a_punch_captured_offline_keeps_its_own_timestamp(self):
        captured = timezone.now() - timedelta(hours=6)
        response = self.check_in(captured_at=captured.isoformat())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        record = Attendance.objects.get()
        self.assertAlmostEqual(
            record.punch_in_at, captured, delta=timedelta(seconds=1)
        )

    def test_a_punch_from_the_future_is_refused(self):
        ahead = timezone.now() + timedelta(hours=2)
        response = self.check_in(captured_at=ahead.isoformat())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('captured_at', response.data)

    def test_a_punch_older_than_the_sync_window_is_refused(self):
        stale = timezone.now() - timedelta(days=30)
        response = self.check_in(captured_at=stale.isoformat())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # ---------------------------------------------------------- GPS validation

    def test_impossible_coordinates_are_refused(self):
        response = self.check_in(latitude='95.0')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('latitude', response.data)

    def test_a_null_island_fix_is_refused(self):
        response = self.check_in(latitude='0.0', longitude='0.0')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_hopeless_gps_accuracy_is_refused(self):
        response = self.check_in(accuracy='450')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('accuracy', response.data)

    @override_settings(ATTENDANCE_SELFIE_REQUIRED=True)
    def test_a_punch_without_a_selfie_is_refused(self):
        response = self.check_in(selfie=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('selfie', response.data)

    @override_settings(ATTENDANCE_SELFIE_REQUIRED=False)
    def test_the_selfie_requirement_can_be_switched_off(self):
        self.assertEqual(self.check_in(selfie=None).status_code, status.HTTP_201_CREATED)

    # ------------------------------------------------------------- geofencing

    def test_a_punch_inside_the_fence_is_flagged_as_inside(self):
        response = self.check_in()
        self.assertTrue(response.data['punch_in_within_fence'])
        self.assertLess(response.data['punch_in_distance_meters'], 300)

    def test_a_punch_outside_the_fence_is_stored_and_flagged(self):
        response = self.check_in(latitude=str(FAR_LAT), longitude=str(FAR_LNG))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertFalse(response.data['punch_in_within_fence'])
        self.assertGreater(response.data['punch_in_distance_meters'], 300)

    @override_settings(ATTENDANCE_ENFORCE_GEOFENCE=True)
    def test_enforcement_refuses_a_punch_outside_the_fence(self):
        response = self.check_in(latitude=str(FAR_LAT), longitude=str(FAR_LNG))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Attendance.objects.count(), 0)

    @override_settings(ATTENDANCE_ENFORCE_GEOFENCE=True)
    def test_enforcement_allows_a_punch_when_no_fence_is_configured(self):
        # Nothing to be outside of. Refusing every punch here would lock a new
        # deployment out of its own attendance module.
        GeoFence.objects.all().delete()
        response = self.check_in(latitude=str(FAR_LAT), longitude=str(FAR_LNG))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data['punch_in_within_fence'])

    def test_haversine_matches_a_known_distance(self):
        # India Gate to Red Fort is about 5.6 km.
        metres = haversine_metres(28.612900, 77.229500, 28.656200, 77.241000)
        self.assertAlmostEqual(metres / 1000, 5.0, delta=0.8)

    # ---------------------------------------------------------------- lateness

    @override_settings(ATTENDANCE_LATE_HOUR=0)
    def test_a_punch_after_the_threshold_is_late(self):
        self.assertTrue(self.check_in().data['is_late'])

    @override_settings(ATTENDANCE_LATE_HOUR=23)
    def test_a_punch_before_the_threshold_is_not_late(self):
        self.assertFalse(self.check_in().data['is_late'])

    # ---------------------------------------------------------------- check-out

    def test_check_out_closes_the_record_and_totals_the_minutes(self):
        captured_in = timezone.now() - timedelta(hours=8)
        self.check_in(captured_at=captured_in.isoformat())

        response = self.check_out()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['is_open'])
        self.assertIsNotNone(response.data['punch_out_selfie'])
        # Server-side arithmetic, not a number the device sent.
        self.assertAlmostEqual(response.data['worked_minutes'], 480, delta=2)

    def test_check_out_without_a_check_in_is_refused(self):
        response = self.check_out()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('no open check-in', response.data['detail'])

    def test_check_out_twice_is_refused(self):
        self.check_in()
        self.check_out()
        self.assertEqual(self.check_out().status_code, status.HTTP_409_CONFLICT)

    def test_check_out_before_check_in_is_refused(self):
        self.check_in()
        earlier = timezone.now() - timedelta(hours=3)
        response = self.check_out(captured_at=earlier.isoformat())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_check_out_requires_the_permission(self):
        self.check_in()
        self.authenticate(self.outsider)
        self.assertEqual(self.check_out().status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------------ read endpoints

    def test_today_is_null_before_the_first_punch(self):
        response = self.client.get(self.url('today'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['attendance'])

    def test_today_returns_the_open_record(self):
        self.check_in()
        response = self.client.get(self.url('today'))
        self.assertTrue(response.data['attendance']['is_open'])

    def test_history_shows_only_the_callers_own_records(self):
        self.check_in()

        self.authenticate(self.outsider)
        response = self.client.get(self.url('history'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 0)

    def test_history_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(
            self.client.get(self.url('history')).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )
