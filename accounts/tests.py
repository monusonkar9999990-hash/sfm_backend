"""End-to-end tests for the authentication endpoints.

These run against a throwaway `test_sfm_db`, created and dropped by the test
runner — the working schema and its data are never touched.
"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError as DjangoValidationError
from django.urls import reverse
from rest_framework import status
from django.test import TestCase
from rest_framework.test import APITestCase
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken

from .models import Department, InviteRequest, Role, Territory, UserTerritory

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


class ReportingLineTests(TestCase):
    """The manager chain is a tree; anything that would make it a ring is
    refused before it can corrupt the materialised paths."""

    def setUp(self):
        self.a = User.objects.create_user(
            'sfm-a', 'Ay One', email='a@corp.com',
            mobile='+919000000001', password=PASSWORD,
        )
        self.b = User.objects.create_user(
            'sfm-b', 'Bee Two', email='b@corp.com',
            mobile='+919000000002', password=PASSWORD,
        )
        self.c = User.objects.create_user(
            'sfm-c', 'Cee Three', email='c@corp.com',
            mobile='+919000000003', password=PASSWORD,
        )

    def test_a_straight_chain_builds_its_paths(self):
        self.b.reporting_manager = self.a
        self.b.save()
        self.c.reporting_manager = self.b
        self.c.save()

        self.assertEqual(
            {u.employee_code for u in self.a.subordinates}, {'SFM-B', 'SFM-C'}
        )
        self.assertEqual({u.employee_code for u in self.b.subordinates}, {'SFM-C'})
        self.assertEqual(list(self.c.subordinates), [])

    def test_a_two_step_loop_is_refused(self):
        self.b.reporting_manager = self.a
        self.b.save()

        self.a.reporting_manager = self.b
        with self.assertRaises(DjangoValidationError):
            self.a.save()

        # And nothing was written: A is still at the top of its own chain.
        self.a.refresh_from_db()
        self.assertIsNone(self.a.reporting_manager)
        self.assertEqual(self.a.manager_path, f'/{self.a.pk}/')

    def test_a_longer_loop_is_refused(self):
        self.b.reporting_manager = self.a
        self.b.save()
        self.c.reporting_manager = self.b
        self.c.save()

        self.a.reporting_manager = self.c
        with self.assertRaises(DjangoValidationError):
            self.a.save()

    def test_moving_a_manager_restamps_the_branch_below(self):
        self.b.reporting_manager = self.a
        self.b.save()
        self.c.reporting_manager = self.b
        self.c.save()

        # B moves out from under A; C must travel with it.
        self.b.reporting_manager = None
        self.b.save()

        self.c.refresh_from_db()
        self.assertTrue(self.c.manager_path.startswith(f'/{self.b.pk}/'))
        self.assertEqual(list(self.a.subordinates), [])


class InviteRequestTests(APITestCase):
    """The one endpoint open to the internet, so the tests care as much about
    what it refuses to reveal as about what it records."""

    @classmethod
    def setUpTestData(cls):
        cls.existing = User.objects.create_user(
            'sfm-9001',
            'Already Here',
            email='already@corp.com',
            mobile='+919876511111',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        cache.clear()

    def url(self):
        return reverse('accounts:request-invite', kwargs={'version': 'v1'})

    def payload(self, **overrides):
        body = {
            'full_name': 'Kiran Rao',
            'employee_code': 'sfm-0142',
            'email': 'kiran.rao@corp.com',
            'mobile': '+919876500099',
            'message': 'Joined the Pune team last week.',
        }
        body.update(overrides)
        return {k: v for k, v in body.items() if v is not None}

    # ------------------------------------------------------------- recording

    def test_a_request_is_recorded_without_creating_anything(self):
        response = self.client.post(self.url(), self.payload(), format='json')
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        invite = InviteRequest.objects.get()
        self.assertEqual(invite.employee_code, 'SFM-0142')
        self.assertEqual(invite.email, 'kiran.rao@corp.com')
        self.assertEqual(invite.status, InviteRequest.Status.PENDING)
        # No account, and nothing that could be used to sign in.
        self.assertFalse(User.objects.filter(employee_code='SFM-0142').exists())
        self.assertNotIn('access', response.data)

    def test_no_authentication_is_needed(self):
        self.client.credentials()
        self.assertEqual(
            self.client.post(self.url(), self.payload(), format='json').status_code,
            status.HTTP_202_ACCEPTED,
        )

    # --------------------------------------------------------- no enumeration

    def test_an_existing_account_gets_the_same_answer(self):
        fresh = self.client.post(self.url(), self.payload(), format='json')
        cache.clear()
        taken = self.client.post(
            self.url(),
            self.payload(employee_code='SFM-9001', email='already@corp.com'),
            format='json',
        )

        # Byte for byte the same reply — the caller cannot learn who is
        # already registered.
        self.assertEqual(taken.status_code, fresh.status_code)
        self.assertEqual(taken.data, fresh.data)
        # And nothing was recorded for the person who already has an account.
        self.assertEqual(InviteRequest.objects.count(), 1)

    def test_a_second_request_from_the_same_person_is_not_duplicated(self):
        self.client.post(self.url(), self.payload(), format='json')
        cache.clear()
        again = self.client.post(self.url(), self.payload(), format='json')

        self.assertEqual(again.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(InviteRequest.objects.count(), 1)

    # ------------------------------------------------------------- validation

    def test_a_malformed_employee_code_is_refused(self):
        response = self.client.post(
            self.url(), self.payload(employee_code='has space'), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('employee_code', response.data)

    def test_a_mobile_outside_e164_is_refused(self):
        response = self.client.post(
            self.url(), self.payload(mobile='9876500099'), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_request_with_no_way_to_reply_is_refused(self):
        response = self.client.post(
            self.url(), self.payload(email='', mobile=''), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_endpoint_is_throttled(self):
        for _ in range(5):
            self.client.post(self.url(), self.payload(), format='json')
        self.assertEqual(
            self.client.post(self.url(), self.payload(), format='json').status_code,
            status.HTTP_429_TOO_MANY_REQUESTS,
        )

    # ---------------------------------------------------------------- review

    def test_approving_creates_an_invited_user_who_cannot_sign_in_yet(self):
        self.client.post(self.url(), self.payload(), format='json')
        invite = InviteRequest.objects.get()

        user = invite.approve(reviewed_by=self.existing, note='Confirmed with HR')

        self.assertEqual(user.employee_code, 'SFM-0142')
        self.assertEqual(user.status, User.Status.INVITED)
        self.assertFalse(user.is_active, 'invited is not active')
        self.assertFalse(user.has_usable_password())

        invite.refresh_from_db()
        self.assertEqual(invite.status, InviteRequest.Status.APPROVED)
        self.assertEqual(invite.created_user, user)
        self.assertEqual(invite.reviewed_by, self.existing)
        self.assertIsNotNone(invite.reviewed_at)

    def test_a_request_cannot_be_reviewed_twice(self):
        self.client.post(self.url(), self.payload(), format='json')
        invite = InviteRequest.objects.get()
        invite.approve(reviewed_by=self.existing)

        with self.assertRaises(DjangoValidationError):
            invite.approve(reviewed_by=self.existing)
        with self.assertRaises(DjangoValidationError):
            invite.reject(reviewed_by=self.existing)

    def test_rejecting_leaves_no_account_behind(self):
        self.client.post(self.url(), self.payload(), format='json')
        invite = InviteRequest.objects.get()

        invite.reject(reviewed_by=self.existing, note='Not on the HR list')

        invite.refresh_from_db()
        self.assertEqual(invite.status, InviteRequest.Status.REJECTED)
        self.assertIsNone(invite.created_user)
        self.assertFalse(User.objects.filter(employee_code='SFM-0142').exists())

    def test_once_approved_the_person_can_request_again_only_as_a_new_row(self):
        # An approved request no longer blocks the "one open request" rule,
        # but the account now exists, so nothing new is recorded either.
        self.client.post(self.url(), self.payload(), format='json')
        InviteRequest.objects.get().approve(reviewed_by=self.existing)
        cache.clear()

        self.client.post(self.url(), self.payload(), format='json')
        self.assertEqual(InviteRequest.objects.count(), 1)
