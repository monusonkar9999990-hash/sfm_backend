"""End-to-end tests for the authentication endpoints.

These run against a throwaway `test_sfm_db`, created and dropped by the test
runner — the working schema and its data are never touched.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

from .models import Department, Role, Territory, UserTerritory

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class AuthEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(
            name='Field Sales Executive', code='field_exec', is_system=True
        )
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts',
                codename__in=['mark_attendance', 'place_orders'],
            )
        )
        cls.department = Department.objects.create(name='Sales', code='SLS')
        cls.territory = Territory.objects.create(
            name='North Zone', code='NZ', kind=Territory.Kind.ZONE
        )
        cls.user = User.objects.create_user(
            'sfm-0002',
            'Alex Mercer',
            email='Alex@Corp.com',
            mobile='+919876543210',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
            department=cls.department,
        )
        UserTerritory.objects.create(
            user=cls.user, territory=cls.territory, is_primary=True
        )

    def setUp(self):
        # Throttle counters live in the cache, not the database, so they
        # survive the per-test rollback. Without this the eleventh login in
        # the whole suite gets a 429 and every later test fails for the wrong
        # reason.
        cache.clear()

    # ------------------------------------------------------------------ helpers

    def url(self, name):
        return reverse(f'accounts:{name}', kwargs={'version': 'v1'})

    def login(self, identifier=None, password=PASSWORD):
        return self.client.post(
            self.url('login'),
            {'identifier': identifier or 'SFM-0002', 'password': password},
            format='json',
        )

    def authenticate(self):
        tokens = self.login().data
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {tokens["access"]}')
        return tokens

    # -------------------------------------------------------------------- login

    def test_login_with_each_identifier(self):
        for identifier in ('+919876543210', '9876543210', 'alex@corp.com', 'SFM-0002'):
            with self.subTest(identifier=identifier):
                response = self.login(identifier)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertIn('access', response.data)
                self.assertIn('refresh', response.data)

    def test_login_returns_the_client_contract(self):
        payload = self.login().data['user']
        self.assertEqual(payload['name'], 'Alex Mercer')
        self.assertEqual(payload['phone'], '+919876543210')
        self.assertEqual(payload['role'], 'Field Sales Executive')
        self.assertEqual(payload['territory'], 'North Zone')
        self.assertEqual(payload['permissions'], ['mark_attendance', 'place_orders'])
        # The Flutter client casts this with `as String`; an int would crash it.
        self.assertIsInstance(payload['id'], str)

    def test_login_with_wrong_password_is_401(self):
        response = self.login(password='wrong-one')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_without_identifier_is_400(self):
        response = self.client.post(
            self.url('login'), {'password': PASSWORD}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_suspended_user_cannot_log_in(self):
        self.user.status = User.Status.SUSPENDED
        self.user.save()
        self.assertEqual(self.login().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_login_is_throttled_after_ten_attempts(self):
        for _ in range(10):
            self.login(password='wrong-one')
        # The eleventh within the minute is refused before the password is
        # ever checked — this is what makes credential stuffing expensive.
        self.assertEqual(self.login().status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    # ------------------------------------------------------------------ refresh

    def test_refresh_rotates_and_blacklists(self):
        tokens = self.login().data
        response = self.client.post(
            self.url('refresh'), {'refresh': tokens['refresh']}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response.data['refresh'], tokens['refresh'])

        replay = self.client.post(
            self.url('refresh'), {'refresh': tokens['refresh']}, format='json'
        )
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    # ----------------------------------------------------------------------- me

    def test_me_requires_a_token(self):
        self.assertEqual(
            self.client.get(self.url('me')).status_code, status.HTTP_401_UNAUTHORIZED
        )

    def test_me_returns_the_signed_in_user(self):
        self.authenticate()
        response = self.client.get(self.url('me'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['employee_code'], 'SFM-0002')
        self.assertEqual(response.data['department'], 'Sales')

    # ------------------------------------------------------------ change password

    def test_change_password_rejects_a_wrong_current_password(self):
        self.authenticate()
        response = self.client.post(
            self.url('change-password'),
            {
                'current_password': 'not-it',
                'new_password': 'An0ther-Pass!9',
                'confirm_password': 'An0ther-Pass!9',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('current_password', response.data)

    def test_change_password_rejects_a_weak_password(self):
        self.authenticate()
        response = self.client.post(
            self.url('change-password'),
            {
                'current_password': PASSWORD,
                'new_password': '12345678',
                'confirm_password': '12345678',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('new_password', response.data)

    def test_change_password_rejects_a_mismatch(self):
        self.authenticate()
        response = self.client.post(
            self.url('change-password'),
            {
                'current_password': PASSWORD,
                'new_password': 'An0ther-Pass!9',
                'confirm_password': 'An0ther-Pass!8',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_change_password_succeeds_and_revokes_old_sessions(self):
        old = self.authenticate()
        response = self.client.post(
            self.url('change-password'),
            {
                'current_password': PASSWORD,
                'new_password': 'An0ther-Pass!9',
                'confirm_password': 'An0ther-Pass!9',
            },
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('access', response.data)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('An0ther-Pass!9'))
        self.assertFalse(self.user.must_change_password)

        # The refresh token issued before the change must no longer work.
        replay = self.client.post(
            self.url('refresh'), {'refresh': old['refresh']}, format='json'
        )
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    # ------------------------------------------------------------------- logout

    def test_logout_blacklists_the_refresh_token(self):
        tokens = self.authenticate()
        response = self.client.post(
            self.url('logout'), {'refresh': tokens['refresh']}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_205_RESET_CONTENT)
        self.assertEqual(BlacklistedToken.objects.count(), 1)

        replay = self.client.post(
            self.url('refresh'), {'refresh': tokens['refresh']}, format='json'
        )
        self.assertEqual(replay.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_logout_refuses_another_users_token(self):
        other = User.objects.create_user(
            'sfm-0003',
            'Sneha Kulkarni',
            email='sneha@corp.com',
            mobile='+919876500044',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )
        stolen = self.client.post(
            self.url('login'),
            {'identifier': other.employee_code, 'password': PASSWORD},
            format='json',
        ).data['refresh']

        self.authenticate()
        response = self.client.post(
            self.url('logout'), {'refresh': stolen}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_logout_requires_authentication(self):
        response = self.client.post(
            self.url('logout'), {'refresh': 'anything'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    # ----------------------------------------------------------------- versioning

    def test_unknown_api_version_is_404(self):
        response = self.client.post(
            '/api/v9/auth/login/',
            {'identifier': 'SFM-0002', 'password': PASSWORD},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
