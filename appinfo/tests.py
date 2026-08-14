"""End-to-end tests for the public configuration endpoints and their admin CRUD."""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.db.utils import OperationalError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from administration.roles import ensure_default_roles

from .models import (
    Announcement,
    AppRelease,
    DocumentKind,
    LegalDocument,
    Platform,
    Priority,
    parse_version,
)

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class AppInfoTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        ensure_default_roles()
        cls.admin_role = Role.objects.get(code='admin')

        cls.admin = User.objects.create_user(
            'sfm-0000', 'Priya Nair', email='priya@corp.com',
            mobile='+919876500011', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.admin_role,
        )
        cls.executive = User.objects.create_user(
            'sfm-0002', 'Alex Mercer', email='alex@corp.com',
            mobile='+919876543210', password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        # The public payloads are cached; a stale one from a previous test
        # would make these pass or fail for the wrong reason.
        cache.clear()

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})

    def publish_policy(self, kind=DocumentKind.PRIVACY, **overrides):
        body = {
            'kind': kind,
            'title': 'Privacy Policy',
            'version': '1.0',
            'effective_date': timezone.localdate() - timedelta(days=1),
            'content': 'We keep what you record and nothing else.',
            'is_published': True,
        }
        body.update(overrides)
        return LegalDocument.objects.create(**body)

    def publish_release(self, **overrides):
        body = {
            'platform': Platform.ANDROID,
            'version': '1.4.0',
            'minimum_supported_version': '1.2.0',
            'force_update': False,
            'download_url': 'https://example.com/app.apk',
            'release_notes': 'Offline sync and a faster dashboard.',
            'is_current': True,
        }
        body.update(overrides)
        return AppRelease.objects.create(**body)


# ------------------------------------------------------------------- privacy


class LegalDocumentPublicTests(AppInfoTestCase):
    def test_the_privacy_policy_is_public(self):
        self.publish_policy()

        response = self.client.get(self.url('appinfo:privacy'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['version'], '1.0')
        self.assertIn('content', response.data)
        self.assertIn('updated_at', response.data)

    def test_the_terms_are_public(self):
        self.publish_policy(
            kind=DocumentKind.TERMS, title='Terms and Conditions'
        )

        response = self.client.get(self.url('appinfo:terms'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'Terms and Conditions')

    def test_nothing_published_is_a_404_not_an_empty_body(self):
        response = self.client.get(self.url('appinfo:privacy'))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_newest_effective_version_wins(self):
        self.publish_policy(version='1.0')
        self.publish_policy(
            version='2.0', effective_date=timezone.localdate()
        )

        response = self.client.get(self.url('appinfo:privacy'))

        self.assertEqual(response.data['version'], '2.0')

    def test_a_future_version_is_not_served_yet(self):
        self.publish_policy(version='1.0')
        self.publish_policy(
            version='9.0', effective_date=timezone.localdate() + timedelta(days=30)
        )

        response = self.client.get(self.url('appinfo:privacy'))

        self.assertEqual(response.data['version'], '1.0')

    def test_an_unpublished_draft_is_not_served(self):
        self.publish_policy(version='1.0')
        self.publish_policy(
            version='2.0',
            effective_date=timezone.localdate(),
            is_published=False,
        )

        response = self.client.get(self.url('appinfo:privacy'))

        self.assertEqual(response.data['version'], '1.0')

    def test_the_two_documents_do_not_mix(self):
        self.publish_policy(kind=DocumentKind.PRIVACY, version='1.0')

        response = self.client.get(self.url('appinfo:terms'))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# --------------------------------------------------------------- app version


class AppVersionTests(AppInfoTestCase):
    def test_the_release_is_public(self):
        self.publish_release()

        response = self.client.get(self.url('appinfo:app-version'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['latest_version'], '1.4.0')
        self.assertEqual(response.data['minimum_supported_version'], '1.2.0')
        self.assertFalse(response.data['force_update'])
        self.assertIn('download_url', response.data)
        self.assertIn('release_notes', response.data)

    def test_no_release_is_a_404(self):
        response = self.client.get(self.url('appinfo:app-version'))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_current_client_is_told_it_is_up_to_date(self):
        self.publish_release()

        response = self.client.get(
            self.url('appinfo:app-version'), {'current_version': '1.4.0'}
        )

        self.assertEqual(response.data['update_status'], 'up_to_date')

    def test_a_newer_client_is_also_up_to_date(self):
        """A tester on a build ahead of the store is not out of date."""
        self.publish_release()

        response = self.client.get(
            self.url('appinfo:app-version'), {'current_version': '2.0.0'}
        )

        self.assertEqual(response.data['update_status'], 'up_to_date')

    def test_an_older_but_supported_client_gets_an_optional_update(self):
        self.publish_release()

        response = self.client.get(
            self.url('appinfo:app-version'), {'current_version': '1.3.0'}
        )

        self.assertEqual(response.data['update_status'], 'update_available')

    def test_a_client_below_the_minimum_must_update(self):
        self.publish_release()

        response = self.client.get(
            self.url('appinfo:app-version'), {'current_version': '1.1.0'}
        )

        self.assertEqual(response.data['update_status'], 'update_required')

    def test_force_update_makes_an_optional_update_mandatory(self):
        self.publish_release(force_update=True)

        response = self.client.get(
            self.url('appinfo:app-version'), {'current_version': '1.3.0'}
        )

        self.assertEqual(response.data['update_status'], 'update_required')

    def test_no_verdict_is_given_without_a_client_version(self):
        self.publish_release()

        response = self.client.get(self.url('appinfo:app-version'))

        self.assertNotIn('update_status', response.data)

    def test_versions_compare_as_numbers_not_as_text(self):
        """`1.10.0` is newer than `1.9.0`, which string comparison gets wrong."""
        self.assertGreater(parse_version('1.10.0'), parse_version('1.9.0'))
        self.assertEqual(parse_version('2'), (2, 0, 0))
        self.assertEqual(parse_version('nonsense'), (0, 0, 0))

    def test_a_ten_point_release_is_not_treated_as_older(self):
        self.publish_release(version='1.10.0', minimum_supported_version='1.9.0')

        response = self.client.get(
            self.url('appinfo:app-version'), {'current_version': '1.10.0'}
        )

        self.assertEqual(response.data['update_status'], 'up_to_date')

    def test_platforms_are_kept_apart(self):
        self.publish_release(platform=Platform.ANDROID, version='1.4.0')
        self.publish_release(
            platform=Platform.IOS, version='1.5.0', minimum_supported_version='1.0.0'
        )

        android = self.client.get(
            self.url('appinfo:app-version'), {'platform': 'android'}
        )
        ios = self.client.get(self.url('appinfo:app-version'), {'platform': 'ios'})

        self.assertEqual(android.data['latest_version'], '1.4.0')
        self.assertEqual(ios.data['latest_version'], '1.5.0')

    def test_an_unknown_platform_is_refused(self):
        response = self.client.get(
            self.url('appinfo:app-version'), {'platform': 'blackberry'}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_only_one_release_per_platform_is_current(self):
        first = self.publish_release(version='1.4.0')
        self.publish_release(version='1.5.0')

        first.refresh_from_db()
        self.assertFalse(first.is_current)
        self.assertEqual(
            AppRelease.objects.filter(
                platform=Platform.ANDROID, is_current=True
            ).count(),
            1,
        )


# ---------------------------------------------------------------- app config


class AppConfigTests(AppInfoTestCase):
    def test_the_config_is_public(self):
        response = self.client.get(self.url('appinfo:app-config'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for key in (
            'company_name', 'support_email', 'support_phone',
            'attendance_radius', 'selfie_required', 'gps_required',
            'max_image_size_mb', 'allowed_image_formats', 'timezone',
            'maintenance_mode', 'feature_flags',
        ):
            self.assertIn(key, response.data, key)

    def test_the_feature_flags_are_listed(self):
        response = self.client.get(self.url('appinfo:app-config'))

        flags = response.data['feature_flags']
        for flag in (
            'products_enabled', 'orders_enabled', 'reports_enabled',
            'offline_sync_enabled', 'site_visits_enabled',
            'customer_registration_enabled',
        ):
            self.assertIn(flag, flags, flag)
            self.assertTrue(flags[flag])

    @override_settings(ATTENDANCE_SELFIE_REQUIRED=False)
    def test_selfie_required_reports_what_the_server_enforces(self):
        """Read from the flag attendance actually checks, not from a copy."""
        response = self.client.get(self.url('appinfo:app-config'))

        self.assertFalse(response.data['selfie_required'])

    @override_settings(TIME_ZONE='Asia/Kolkata')
    def test_the_timezone_is_the_servers_own(self):
        response = self.client.get(self.url('appinfo:app-config'))

        self.assertEqual(response.data['timezone'], 'Asia/Kolkata')

    def test_the_response_says_which_values_the_server_enforces(self):
        response = self.client.get(self.url('appinfo:app-config'))

        enforced = response.data['enforced_by_server']
        self.assertIn('maintenance_mode', enforced)
        self.assertIn('selfie_required', enforced)
        # A client budget, not a server limit — and the payload says so.
        self.assertNotIn('max_image_size_mb', enforced)

    def test_image_formats_come_back_as_a_list(self):
        response = self.client.get(self.url('appinfo:app-config'))

        self.assertEqual(response.data['allowed_image_formats'], ['jpg', 'jpeg', 'png'])

    def test_a_settings_change_shows_up_in_the_config(self):
        self.client.force_authenticate(self.admin)
        self.client.put(
            reverse('administration:settings', kwargs={'version': 'v1'}),
            {'company_name': 'Acme Cement Co'},
            format='json',
        )
        self.client.force_authenticate(None)

        response = self.client.get(self.url('appinfo:app-config'))

        self.assertEqual(response.data['company_name'], 'Acme Cement Co')

    def test_the_server_time_is_included_and_never_cached(self):
        first = self.client.get(self.url('appinfo:app-config'))
        second = self.client.get(self.url('appinfo:app-config'))

        self.assertIn('server_time', first.data)
        self.assertNotEqual(first.data['server_time'], second.data['server_time'])


# -------------------------------------------------------------- announcements


class AnnouncementPublicTests(AppInfoTestCase):
    def make(self, **overrides):
        body = {
            'title': 'Scheduled maintenance',
            'message': 'The system will be down on Sunday morning.',
            'priority': Priority.NORMAL,
            'start_date': timezone.now() - timedelta(hours=1),
            'is_active': True,
        }
        body.update(overrides)
        return Announcement.objects.create(**body)

    def test_live_announcements_are_public(self):
        self.make()

        response = self.client.get(self.url('appinfo:announcements'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(
            response.data['results'][0]['title'], 'Scheduled maintenance'
        )

    def test_an_inactive_announcement_is_not_served(self):
        self.make(is_active=False)

        response = self.client.get(self.url('appinfo:announcements'))

        self.assertEqual(response.data['count'], 0)

    def test_one_that_has_not_started_is_not_served(self):
        self.make(start_date=timezone.now() + timedelta(days=1))

        response = self.client.get(self.url('appinfo:announcements'))

        self.assertEqual(response.data['count'], 0)

    def test_an_expired_announcement_is_not_served(self):
        self.make(
            start_date=timezone.now() - timedelta(days=2),
            end_date=timezone.now() - timedelta(days=1),
        )

        response = self.client.get(self.url('appinfo:announcements'))

        self.assertEqual(response.data['count'], 0)

    def test_one_with_no_end_date_runs_until_switched_off(self):
        self.make(end_date=None)

        response = self.client.get(self.url('appinfo:announcements'))

        self.assertEqual(response.data['count'], 1)

    def test_the_most_urgent_comes_first(self):
        """Not alphabetical: `critical` must outrank `normal`."""
        self.make(title='Low', priority=Priority.LOW)
        self.make(title='Normal', priority=Priority.NORMAL)
        self.make(title='Critical', priority=Priority.CRITICAL)
        self.make(title='High', priority=Priority.HIGH)

        response = self.client.get(self.url('appinfo:announcements'))

        titles = [row['title'] for row in response.data['results']]
        self.assertEqual(titles, ['Critical', 'High', 'Normal', 'Low'])

    def test_an_empty_list_is_not_an_error(self):
        response = self.client.get(self.url('appinfo:announcements'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])


# -------------------------------------------------------------------- access


class PublicAccessTests(AppInfoTestCase):
    def test_every_public_endpoint_works_without_a_token(self):
        self.publish_policy()
        self.publish_policy(kind=DocumentKind.TERMS, version='t1')
        self.publish_release()

        for name in (
            'appinfo:privacy', 'appinfo:terms', 'appinfo:app-version',
            'appinfo:app-config', 'appinfo:announcements',
        ):
            with self.subTest(endpoint=name):
                response = self.client.get(self.url(name))
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_an_expired_token_does_not_break_a_public_endpoint(self):
        """The moment a client most needs /app-config/ is when its token has
        just expired, so these endpoints do not authenticate at all."""
        response = self.client.get(
            self.url('appinfo:app-config'),
            HTTP_AUTHORIZATION='Bearer not-a-real-token',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_public_endpoints_are_read_only(self):
        for method in (self.client.post, self.client.put, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(self.url('appinfo:app-config'), {}, format='json')
                self.assertEqual(
                    response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED
                )

    def test_they_stay_open_during_maintenance(self):
        """Closing them would hide the very message explaining the closure."""
        self.client.force_authenticate(self.admin)
        self.client.put(
            reverse('administration:settings', kwargs={'version': 'v1'}),
            {'maintenance_mode': True},
            format='json',
        )
        self.client.force_authenticate(None)

        try:
            response = self.client.get(self.url('appinfo:app-config'))
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertTrue(response.data['maintenance_mode'])
        finally:
            self.client.force_authenticate(self.admin)
            self.client.put(
                reverse('administration:settings', kwargs={'version': 'v1'}),
                {'maintenance_mode': False},
                format='json',
            )


# ---------------------------------------------------------------- admin CRUD


class AdminCrudTests(AppInfoTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)

    # ------------------------------------------------------ legal documents

    def test_a_policy_can_be_published(self):
        response = self.client.post(
            self.url('appinfo-admin:legal-list'),
            {
                'kind': DocumentKind.PRIVACY,
                'title': 'Privacy Policy',
                'version': '1.0',
                'effective_date': timezone.localdate().isoformat(),
                'content': 'What we keep and why.',
                'is_published': True,
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['is_current'])
        self.assertEqual(response.data['created_by'], self.admin.pk)

    def test_a_duplicate_version_is_refused(self):
        self.publish_policy(version='1.0')

        response = self.client.post(
            self.url('appinfo-admin:legal-list'),
            {
                'kind': DocumentKind.PRIVACY,
                'title': 'Privacy Policy',
                'version': '1.0',
                'effective_date': timezone.localdate().isoformat(),
                'content': 'Again.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('version', response.data)

    def test_documents_can_be_filtered_by_kind(self):
        self.publish_policy(kind=DocumentKind.PRIVACY, version='p1')
        self.publish_policy(kind=DocumentKind.TERMS, version='t1')

        response = self.client.get(
            self.url('appinfo-admin:legal-list'), {'kind': 'terms'}
        )

        self.assertEqual(response.data['count'], 1)

    def test_a_policy_can_be_corrected(self):
        document = self.publish_policy()

        response = self.client.patch(
            self.url('appinfo-admin:legal-detail', pk=document.pk),
            {'content': 'Corrected wording.'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['content'], 'Corrected wording.')

    def test_the_version_in_force_cannot_be_deleted(self):
        """Somebody agreed to it; the record has to survive."""
        document = self.publish_policy()

        response = self.client.delete(
            self.url('appinfo-admin:legal-detail', pk=document.pk)
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(LegalDocument.objects.filter(pk=document.pk).exists())

    def test_a_superseded_version_can_be_deleted(self):
        old = self.publish_policy(version='1.0')
        self.publish_policy(version='2.0', effective_date=timezone.localdate())

        response = self.client.delete(
            self.url('appinfo-admin:legal-detail', pk=old.pk)
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    # ---------------------------------------------------------- app releases

    def test_a_release_can_be_published(self):
        response = self.client.post(
            self.url('appinfo-admin:release-list'),
            {
                'platform': Platform.ANDROID,
                'version': '2.0.0',
                'minimum_supported_version': '1.5.0',
                'force_update': True,
                'download_url': 'https://example.com/app.apk',
                'release_notes': 'Everything is faster.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['is_current'])

    def test_a_minimum_above_the_release_is_refused(self):
        """It would tell every client to update to a version that does not
        exist."""
        response = self.client.post(
            self.url('appinfo-admin:release-list'),
            {
                'platform': Platform.ANDROID,
                'version': '1.0.0',
                'minimum_supported_version': '2.0.0',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('minimum_supported_version', response.data)

    def test_a_duplicate_version_for_a_platform_is_refused(self):
        self.publish_release(version='1.4.0')

        response = self.client.post(
            self.url('appinfo-admin:release-list'),
            {
                'platform': Platform.ANDROID,
                'version': '1.4.0',
                'minimum_supported_version': '1.0.0',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --------------------------------------------------------- announcements

    def test_an_announcement_can_be_created(self):
        response = self.client.post(
            self.url('appinfo-admin:announcement-list'),
            {
                'title': 'New price list',
                'message': 'Cement rates change on Monday.',
                'priority': Priority.HIGH,
                'start_date': timezone.now().isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['is_live'])
        self.assertEqual(response.data['created_by'], self.admin.pk)

    def test_an_announcement_that_ends_before_it_starts_is_refused(self):
        response = self.client.post(
            self.url('appinfo-admin:announcement-list'),
            {
                'title': 'Backwards',
                'message': 'Impossible.',
                'start_date': timezone.now().isoformat(),
                'end_date': (timezone.now() - timedelta(days=1)).isoformat(),
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('end_date', response.data)

    def test_an_announcement_can_be_switched_off(self):
        announcement = Announcement.objects.create(
            title='Temporary', message='For now.',
            start_date=timezone.now() - timedelta(hours=1),
        )

        self.client.patch(
            self.url('appinfo-admin:announcement-detail', pk=announcement.pk),
            {'is_active': False},
            format='json',
        )

        self.client.force_authenticate(None)
        public = self.client.get(self.url('appinfo:announcements'))
        self.assertEqual(public.data['count'], 0)

    def test_an_announcement_can_be_deleted(self):
        announcement = Announcement.objects.create(
            title='Gone', message='Bye.', start_date=timezone.now()
        )

        response = self.client.delete(
            self.url('appinfo-admin:announcement-detail', pk=announcement.pk)
        )

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class AdminAccessTests(AppInfoTestCase):
    def test_admin_crud_needs_a_token(self):
        for name in (
            'appinfo-admin:legal-list',
            'appinfo-admin:release-list',
            'appinfo-admin:announcement-list',
        ):
            with self.subTest(endpoint=name):
                response = self.client.get(self.url(name))
                self.assertEqual(
                    response.status_code, status.HTTP_401_UNAUTHORIZED
                )

    def test_an_executive_cannot_read_or_write_admin_crud(self):
        self.client.force_authenticate(self.executive)

        for name in (
            'appinfo-admin:legal-list',
            'appinfo-admin:release-list',
            'appinfo-admin:announcement-list',
        ):
            with self.subTest(endpoint=name):
                self.assertEqual(
                    self.client.get(self.url(name)).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_an_executive_cannot_publish_a_policy(self):
        self.client.force_authenticate(self.executive)

        response = self.client.post(
            self.url('appinfo-admin:legal-list'),
            {
                'kind': DocumentKind.PRIVACY, 'title': 'Mine', 'version': '9.9',
                'effective_date': timezone.localdate().isoformat(),
                'content': 'Rewritten.',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(LegalDocument.objects.count(), 0)


# ------------------------------------------------------------------- caching


class CacheTests(AppInfoTestCase):
    def test_a_second_read_does_not_hit_the_database(self):
        self.publish_policy()
        self.client.get(self.url('appinfo:privacy'))

        with self.assertNumQueries(0):
            self.client.get(self.url('appinfo:privacy'))

    def test_publishing_retires_the_cached_copy(self):
        self.publish_policy(version='1.0')
        first = self.client.get(self.url('appinfo:privacy'))
        self.assertEqual(first.data['version'], '1.0')

        self.client.force_authenticate(self.admin)
        self.client.post(
            self.url('appinfo-admin:legal-list'),
            {
                'kind': DocumentKind.PRIVACY,
                'title': 'Privacy Policy',
                'version': '2.0',
                'effective_date': timezone.localdate().isoformat(),
                'content': 'Newer.',
                'is_published': True,
            },
            format='json',
        )
        self.client.force_authenticate(None)

        second = self.client.get(self.url('appinfo:privacy'))
        self.assertEqual(second.data['version'], '2.0')

    def test_an_announcement_change_retires_the_cached_list(self):
        self.client.get(self.url('appinfo:announcements'))

        self.client.force_authenticate(self.admin)
        self.client.post(
            self.url('appinfo-admin:announcement-list'),
            {
                'title': 'Fresh', 'message': 'Just published.',
                'start_date': timezone.now().isoformat(),
            },
            format='json',
        )
        self.client.force_authenticate(None)

        response = self.client.get(self.url('appinfo:announcements'))
        self.assertEqual(response.data['count'], 1)

    def test_the_config_cache_is_retired_by_a_settings_change(self):
        self.client.get(self.url('appinfo:app-config'))

        self.client.force_authenticate(self.admin)
        self.client.put(
            reverse('administration:settings', kwargs={'version': 'v1'}),
            {'support_email': 'help@acme.example'},
            format='json',
        )
        self.client.force_authenticate(None)

        response = self.client.get(self.url('appinfo:app-config'))
        self.assertEqual(response.data['support_email'], 'help@acme.example')


class HealthTests(APITestCase):
    """The endpoint a load balancer polls.

    Added for production deployment: without one, an orchestrator can only ask
    "did the TCP connection open", which stays true on an instance whose
    database has gone away.
    """

    def url(self):
        return reverse('appinfo:health', kwargs={'version': 'v1'})

    def test_a_healthy_instance_answers_ok(self):
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'ok')
        self.assertEqual(response.data['database'], 'ok')

    def test_it_needs_no_token(self):
        # A health check that required authentication would be useless to a
        # load balancer, and would fail exactly when the auth backend broke.
        self.client.credentials()
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_it_says_nothing_about_the_host_or_the_driver(self):
        body = str(self.client.get(self.url()).data)

        # Unauthenticated, so it must not become a reconnaissance tool.
        for leak in ('mysql', 'password', '127.0.0.1', 'sfm_user', 'Traceback'):
            self.assertNotIn(leak.lower(), body.lower())

    def test_a_dead_database_reports_503(self):
        # 200 with a dead database keeps a broken instance in rotation, which
        # is the whole failure this endpoint exists to catch.
        with patch(
            'appinfo.views.connection.ensure_connection',
            side_effect=OperationalError('gone'),
        ):
            response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertEqual(response.data['status'], 'degraded')
        self.assertEqual(response.data['database'], 'unavailable')
