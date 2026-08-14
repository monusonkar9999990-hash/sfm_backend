"""End-to-end tests for the administration endpoints."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.contrib.auth.hashers import make_password
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Department, Designation, InviteRequest, Role, Territory
from customers.models import Customer, CustomerType

from .models import AppSetting, AuditAction, AuditLog
from .roles import DEFAULT_ROLES, ensure_default_roles
from .settings_registry import REGISTRY

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class AdminTestCase(APITestCase):
    """An administrator who may do everything, and an executive who may not."""

    @classmethod
    def setUpTestData(cls):
        ensure_default_roles()

        cls.admin_role = Role.objects.get(code='admin')
        cls.exec_role = Role.objects.get(code='sales_executive')
        cls.super_role = Role.objects.get(code='super_admin')

        cls.admin = User.objects.create_user(
            'sfm-0000', 'Priya Nair', email='priya@corp.com',
            mobile='+919876500011', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.admin_role,
        )
        cls.executive = User.objects.create_user(
            'sfm-0002', 'Alex Mercer', email='alex@corp.com',
            mobile='+919876543210', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.exec_role,
        )

        cls.department = Department.objects.create(name='Sales', code='SLS')
        cls.designation = Designation.objects.create(
            name='Field Sales Executive', code='FSE'
        )
        cls.territory = Territory.objects.create(name='Delhi NCR', code='DEL')

    def setUp(self):
        cache.clear()
        self.client.force_authenticate(self.admin)

    def url(self, name, **kwargs):
        return reverse(name, kwargs={'version': 'v1', **kwargs})


# ------------------------------------------------------------------ employees


class EmployeeTests(AdminTestCase):
    @property
    def list_url(self):
        return self.url('administration:employee-list')

    def detail_url(self, pk):
        return self.url('administration:employee-detail', pk=pk)

    def payload(self, **overrides):
        body = {
            'employee_code': 'SFM-0100',
            'full_name': 'Neha Sharma',
            'email': 'neha@corp.com',
            'mobile': '+919876500100',
            'status': User.Status.ACTIVE,
            'role': str(self.exec_role.pk),
            'department': str(self.department.pk),
            'designation': str(self.designation.pk),
        }
        body.update(overrides)
        return body

    def test_an_employee_can_be_created(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['employee_code'], 'SFM-0100')
        self.assertEqual(response.data['role_code'], 'sales_executive')
        self.assertTrue(User.objects.filter(employee_code='SFM-0100').exists())

    def test_a_new_account_must_choose_its_own_password(self):
        """An administrator never types somebody else's password."""
        self.client.post(self.list_url, self.payload(), format='json')

        employee = User.objects.get(employee_code='SFM-0100')
        self.assertTrue(employee.must_change_password)
        self.assertFalse(employee.has_usable_password())

    def test_the_permission_list_follows_the_role(self):
        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertIn('place_orders', response.data['permissions'])
        self.assertNotIn('manage_users', response.data['permissions'])

    def test_a_duplicate_employee_code_is_refused(self):
        response = self.client.post(
            self.list_url, self.payload(employee_code='SFM-0002'), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('employee_code', response.data)

    def test_an_employee_needs_a_way_to_sign_in(self):
        response = self.client.post(
            self.list_url, self.payload(email='', mobile=''), format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_territories_can_be_assigned(self):
        response = self.client.post(
            self.list_url,
            self.payload(primary_territory=str(self.territory.pk)),
            format='json',
        )

        self.assertEqual(len(response.data['territories']), 1)
        self.assertTrue(response.data['territories'][0]['is_primary'])

    def test_an_employee_can_be_read(self):
        response = self.client.get(self.detail_url(self.executive.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['employee_code'], 'SFM-0002')

    def test_an_employee_can_be_updated(self):
        response = self.client.patch(
            self.detail_url(self.executive.pk),
            {'full_name': 'Alex R Mercer'},
            format='json',
        )

        self.executive.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.executive.full_name, 'Alex R Mercer')

    def test_a_role_can_be_changed(self):
        self.client.patch(
            self.detail_url(self.executive.pk),
            {'role': str(self.admin_role.pk)},
            format='json',
        )

        self.executive.refresh_from_db()
        self.assertEqual(self.executive.role, self.admin_role)
        # The Group behind the role follows, so has_perm changes with it.
        self.assertTrue(self.executive.has_perm('accounts.manage_users'))

    def test_nobody_can_report_to_themselves(self):
        response = self.client.patch(
            self.detail_url(self.executive.pk),
            {'reporting_manager': str(self.executive.pk)},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_delete_suspends_rather_than_erases(self):
        response = self.client.delete(self.detail_url(self.executive.pk))

        self.executive.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.executive.status, User.Status.SUSPENDED)
        self.assertFalse(self.executive.is_active)
        self.assertTrue(User.objects.filter(pk=self.executive.pk).exists())

    def test_an_administrator_cannot_suspend_themselves(self):
        response = self.client.delete(self.detail_url(self.admin.pk))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_an_account_can_be_suspended_and_reactivated(self):
        url = self.url('administration:employee-status', pk=self.executive.pk)

        suspended = self.client.post(
            url, {'status': User.Status.SUSPENDED}, format='json'
        )
        self.executive.refresh_from_db()
        self.assertEqual(suspended.status_code, status.HTTP_200_OK)
        self.assertFalse(self.executive.is_active)

        self.client.post(url, {'status': User.Status.ACTIVE}, format='json')
        self.executive.refresh_from_db()
        self.assertTrue(self.executive.is_active)

    def test_the_list_is_paginated(self):
        response = self.client.get(self.list_url)

        self.assertIn('results', response.data)
        self.assertEqual(response.data['count'], 2)

    def test_the_list_can_be_searched(self):
        response = self.client.get(self.list_url, {'search': 'Mercer'})

        self.assertEqual(response.data['count'], 1)

    def test_the_list_can_be_filtered_by_role(self):
        by_code = self.client.get(self.list_url, {'role': 'sales_executive'})
        by_id = self.client.get(self.list_url, {'role': str(self.exec_role.pk)})

        self.assertEqual(by_code.data['count'], 1)
        self.assertEqual(by_id.data['count'], 1)

    def test_the_list_can_be_filtered_by_status(self):
        self.client.delete(self.detail_url(self.executive.pk))

        active = self.client.get(self.list_url, {'status': User.Status.ACTIVE})
        suspended = self.client.get(
            self.list_url, {'status': User.Status.SUSPENDED}
        )

        self.assertEqual(active.data['count'], 1)
        self.assertEqual(suspended.data['count'], 1)

    def test_the_list_can_be_ordered(self):
        response = self.client.get(self.list_url, {'ordering': 'employee_code'})

        codes = [row['employee_code'] for row in response.data['results']]
        self.assertEqual(codes, sorted(codes))

    def test_an_executive_cannot_read_the_staff_list(self):
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_executive_cannot_create_an_employee(self):
        self.client.force_authenticate(self.executive)

        response = self.client.post(self.list_url, self.payload(), format='json')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_access_is_refused(self):
        self.client.force_authenticate(None)

        self.assertEqual(
            self.client.get(self.list_url).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )


# ---------------------------------------------------------------------- roles


class RoleTests(AdminTestCase):
    @property
    def list_url(self):
        return self.url('administration:role-list')

    def detail_url(self, pk):
        return self.url('administration:role-detail', pk=pk)

    def test_the_four_default_roles_exist(self):
        codes = set(Role.objects.values_list('code', flat=True))

        for definition in DEFAULT_ROLES:
            self.assertIn(definition['code'], codes)

    def test_super_admin_holds_every_permission(self):
        from .roles import business_permissions

        self.assertEqual(
            set(self.super_role.permission_codenames),
            {p.codename for p in business_permissions()},
        )

    def test_ensure_default_roles_is_idempotent(self):
        before = Role.objects.count()

        ensure_default_roles()

        self.assertEqual(Role.objects.count(), before)

    def test_a_role_can_be_created_with_permissions(self):
        response = self.client.post(
            self.list_url,
            {
                'name': 'Auditor',
                'code': 'auditor',
                'description': 'Reads the trail.',
                'permissions': ['view_audit_logs', 'view_reports'],
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            sorted(response.data['permissions']),
            ['view_audit_logs', 'view_reports'],
        )

    def test_an_unknown_permission_is_refused(self):
        response = self.client.post(
            self.list_url,
            {'name': 'Wizard', 'code': 'wizard', 'permissions': ['cast_spells']},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('permissions', response.data)

    def test_a_duplicate_code_is_refused(self):
        response = self.client.post(
            self.list_url, {'name': 'Another Admin', 'code': 'admin'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_role_can_be_renamed(self):
        role = Role.objects.create(name='Temp', code='temp')

        response = self.client.patch(
            self.detail_url(role.pk), {'name': 'Temporary'}, format='json'
        )

        role.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(role.name, 'Temporary')
        # The Group's name follows, so the two lists never drift.
        self.assertEqual(role.group.name, 'Temporary')

    def test_the_user_count_is_reported(self):
        response = self.client.get(self.detail_url(self.exec_role.pk))

        self.assertEqual(response.data['user_count'], 1)

    def test_a_system_role_cannot_be_deleted(self):
        response = self.client.delete(self.detail_url(self.admin_role.pk))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Role.objects.filter(pk=self.admin_role.pk).exists())

    def test_a_role_in_use_cannot_be_deleted(self):
        role = Role.objects.create(name='Temp', code='temp')
        self.executive.role = role
        self.executive.save()

        response = self.client.delete(self.detail_url(role.pk))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('employee', str(response.data).lower())

    def test_an_unused_custom_role_can_be_deleted(self):
        role = Role.objects.create(name='Temp', code='temp')

        response = self.client.delete(self.detail_url(role.pk))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Role.objects.filter(pk=role.pk).exists())

    def test_anyone_signed_in_may_read_the_role_list(self):
        """A client needs it to draw a picker; a role name is not sensitive."""
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_an_executive_cannot_create_a_role(self):
        self.client.force_authenticate(self.executive)

        response = self.client.post(
            self.list_url, {'name': 'Mine', 'code': 'mine'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class PermissionTests(AdminTestCase):
    def test_every_permission_is_listed(self):
        response = self.client.get(self.url('administration:permission-list'))

        codenames = {row['codename'] for row in response.data}
        self.assertIn('manage_users', codenames)
        self.assertIn('approve_registrations', codenames)
        self.assertIn('view_audit_logs', codenames)

    def test_the_permission_list_is_not_paginated(self):
        response = self.client.get(self.url('administration:permission-list'))

        self.assertIsInstance(response.data, list)

    def test_a_roles_permissions_can_be_replaced(self):
        role = Role.objects.create(name='Temp', code='temp')
        role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='view_reports'
            )
        )

        response = self.client.put(
            self.url('administration:role-permissions', pk=role.pk),
            {'permissions': ['place_orders', 'view_pricing']},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            sorted(response.data['permissions']), ['place_orders', 'view_pricing']
        )

    def test_replacing_permissions_changes_what_users_can_do(self):
        self.assertFalse(self.executive.has_perm('accounts.manage_users'))

        self.client.put(
            self.url('administration:role-permissions', pk=self.exec_role.pk),
            {'permissions': ['manage_users']},
            format='json',
        )

        # A fresh instance: Django caches permissions on the one in memory.
        refreshed = User.objects.get(pk=self.executive.pk)
        self.assertTrue(refreshed.has_perm('accounts.manage_users'))

    def test_an_unknown_permission_is_refused(self):
        response = self.client.put(
            self.url('administration:role-permissions', pk=self.exec_role.pk),
            {'permissions': ['rule_the_world']},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_change_is_audited_with_what_moved(self):
        self.client.put(
            self.url('administration:role-permissions', pk=self.exec_role.pk),
            {'permissions': ['view_reports']},
            format='json',
        )

        entry = AuditLog.objects.filter(
            action=AuditAction.PERMISSIONS_UPDATE
        ).latest('created_at')
        self.assertIn('place_orders', entry.changes['revoked'])
        self.assertEqual(entry.actor, self.admin)

    def test_an_executive_cannot_grant_themselves_permissions(self):
        self.client.force_authenticate(self.executive)

        response = self.client.put(
            self.url('administration:role-permissions', pk=self.exec_role.pk),
            {'permissions': ['manage_users']},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ------------------------------------------------------------ invite requests


class InviteRequestTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.invite = InviteRequest.objects.create(
            full_name='Ravi Kumar',
            employee_code='SFM-0200',
            email='ravi@corp.com',
            mobile='+919876500200',
            message='Joined the Noida team last week.',
        )

    @property
    def list_url(self):
        return self.url('administration:invite-list')

    def approve_url(self, pk):
        return self.url('administration:invite-approve', pk=pk)

    def reject_url(self, pk):
        return self.url('administration:invite-reject', pk=pk)

    def test_pending_requests_are_listed(self):
        response = self.client.get(self.list_url, {'status': 'pending'})

        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['full_name'], 'Ravi Kumar')

    def test_approval_creates_the_account(self):
        response = self.client.post(
            self.approve_url(self.invite.pk),
            {'role': str(self.exec_role.pk)},
            format='json',
        )

        self.invite.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.invite.status, InviteRequest.Status.APPROVED)
        self.assertIsNotNone(self.invite.created_user)

        employee = User.objects.get(employee_code='SFM-0200')
        self.assertEqual(employee.status, User.Status.ACTIVE)
        self.assertEqual(employee.role, self.exec_role)

    def test_the_approved_account_sets_its_own_password(self):
        self.client.post(self.approve_url(self.invite.pk), {}, format='json')

        employee = User.objects.get(employee_code='SFM-0200')
        self.assertTrue(employee.must_change_password)
        self.assertFalse(employee.has_usable_password())

    def test_a_password_chosen_when_applying_survives_approval(self):
        chosen = 'Ravi-Pass!42'
        self.invite.password_hash = make_password(chosen)
        self.invite.save(update_fields=['password_hash'])

        response = self.client.post(self.approve_url(self.invite.pk), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        employee = User.objects.get(employee_code='SFM-0200')
        self.assertTrue(employee.check_password(chosen))
        self.assertFalse(employee.must_change_password)
        self.assertEqual(employee.status, User.Status.ACTIVE)
        # The administrator is told what to say, and it is not a password.
        self.assertNotIn(chosen, str(response.data))
        self.assertIn('chose', response.data['notification']['detail'])

    def test_the_response_says_no_notification_was_sent(self):
        """No mail backend is configured, and the response says so rather than
        implying an email went out."""
        response = self.client.post(
            self.approve_url(self.invite.pk), {}, format='json'
        )

        self.assertFalse(response.data['notification']['sent'])
        self.assertIn('no mail', response.data['notification']['detail'].lower())

    def test_the_administrator_can_correct_the_claimed_code(self):
        response = self.client.post(
            self.approve_url(self.invite.pk),
            {'employee_code': 'SFM-0201'},
            format='json',
        )

        self.assertEqual(response.data['employee']['employee_code'], 'SFM-0201')
        self.assertFalse(User.objects.filter(employee_code='SFM-0200').exists())

    def test_a_code_already_in_use_is_refused(self):
        response = self.client.post(
            self.approve_url(self.invite.pk),
            {'employee_code': 'SFM-0002'},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.invite.refresh_from_db()
        self.assertTrue(self.invite.is_pending)

    def test_approval_records_who_reviewed_it(self):
        self.client.post(self.approve_url(self.invite.pk), {}, format='json')

        self.invite.refresh_from_db()
        self.assertEqual(self.invite.reviewed_by, self.admin)
        self.assertIsNotNone(self.invite.reviewed_at)

    def test_a_request_cannot_be_approved_twice(self):
        self.client.post(self.approve_url(self.invite.pk), {}, format='json')

        response = self.client.post(
            self.approve_url(self.invite.pk), {}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(User.objects.filter(employee_code='SFM-0200').count(), 1)

    def test_rejection_stores_the_reason(self):
        response = self.client.post(
            self.reject_url(self.invite.pk),
            {'reason': 'Not an employee of this company'},
            format='json',
        )

        self.invite.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.invite.status, InviteRequest.Status.REJECTED)
        self.assertEqual(
            self.invite.review_note, 'Not an employee of this company'
        )
        self.assertFalse(User.objects.filter(employee_code='SFM-0200').exists())

    def test_a_rejection_needs_a_reason(self):
        response = self.client.post(self.reject_url(self.invite.pk), {}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_rejected_request_cannot_then_be_approved(self):
        self.client.post(
            self.reject_url(self.invite.pk), {'reason': 'Not an employee'},
            format='json',
        )

        response = self.client.post(
            self.approve_url(self.invite.pk), {}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_approval_and_rejection_are_audited(self):
        second = InviteRequest.objects.create(
            full_name='Meera Iyer', employee_code='SFM-0300', email='meera@corp.com'
        )

        self.client.post(self.approve_url(self.invite.pk), {}, format='json')
        self.client.post(
            self.reject_url(second.pk), {'reason': 'Duplicate request'},
            format='json',
        )

        self.assertTrue(
            AuditLog.objects.filter(action=AuditAction.APPROVE).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(action=AuditAction.REJECT).exists()
        )

    def test_an_executive_cannot_approve(self):
        self.client.force_authenticate(self.executive)

        response = self.client.post(
            self.approve_url(self.invite.pk), {}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ------------------------------------------------------------------- settings


class SettingsTests(AdminTestCase):
    @property
    def settings_url(self):
        return self.url('administration:settings')

    def test_every_setting_is_returned_with_its_default(self):
        response = self.client.get(self.settings_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data['settings']), set(REGISTRY))
        self.assertFalse(
            response.data['settings']['maintenance_mode']['is_overridden']
        )

    def test_each_setting_declares_whether_the_server_enforces_it(self):
        """The distinction is published, so nobody flips an advisory switch
        expecting the API to change behaviour."""
        response = self.client.get(self.settings_url)

        settings_block = response.data['settings']
        self.assertEqual(settings_block['maintenance_mode']['effect'], 'enforced')
        self.assertEqual(
            settings_block['default_gst_percent']['effect'], 'advisory'
        )

    def test_a_setting_can_be_changed(self):
        response = self.client.put(
            self.settings_url, {'default_working_hours': 9}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        block = response.data['settings']['default_working_hours']
        self.assertEqual(block['value'], 9)
        self.assertTrue(block['is_overridden'])
        self.assertTrue(AppSetting.objects.filter(key='default_working_hours').exists())

    def test_a_partial_update_leaves_the_rest_alone(self):
        self.client.put(self.settings_url, {'app_version': '2.0.0'}, format='json')

        response = self.client.get(self.settings_url)
        self.assertEqual(response.data['settings']['app_version']['value'], '2.0.0')
        self.assertFalse(
            response.data['settings']['default_gst_percent']['is_overridden']
        )

    def test_values_are_coerced_to_their_declared_type(self):
        response = self.client.put(
            self.settings_url,
            {'maintenance_mode': 'true', 'low_stock_threshold': '40'},
            format='json',
        )

        settings_block = response.data['settings']
        self.assertIs(settings_block['maintenance_mode']['value'], True)
        self.assertEqual(settings_block['low_stock_threshold']['value'], 40)

    def test_an_unknown_setting_is_refused(self):
        response = self.client.put(
            self.settings_url, {'enable_time_travel': True}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_value_outside_its_bounds_is_refused(self):
        response = self.client.put(
            self.settings_url, {'default_gst_percent': 500}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('default_gst_percent', response.data)

    def test_a_value_of_the_wrong_type_is_refused(self):
        response = self.client.put(
            self.settings_url, {'low_stock_threshold': 'lots'}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_settings_change_is_audited_with_before_and_after(self):
        self.client.put(
            self.settings_url, {'default_working_hours': 9}, format='json'
        )

        entry = AuditLog.objects.filter(
            action=AuditAction.SETTINGS_UPDATE
        ).latest('created_at')
        self.assertEqual(entry.changes['before']['default_working_hours'], 8.0)
        self.assertEqual(entry.changes['after']['default_working_hours'], 9)

    def test_an_executive_cannot_read_or_change_settings(self):
        self.client.force_authenticate(self.executive)

        self.assertEqual(
            self.client.get(self.settings_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.put(
                self.settings_url, {'app_version': '9.9.9'}, format='json'
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class MaintenanceModeTests(AdminTestCase):
    """The one setting the server itself enforces."""

    def enable(self):
        self.client.put(
            self.url('administration:settings'),
            {'maintenance_mode': True},
            format='json',
        )

    def test_the_api_closes_for_ordinary_users(self):
        self.enable()
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.url('administration:role-list'))

        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()['maintenance_mode'])

    def test_administrators_are_let_through(self):
        """Somebody has to be able to switch it back off."""
        self.enable()

        response = self.client.get(self.url('administration:settings'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_it_can_be_switched_back_off(self):
        self.enable()
        self.client.put(
            self.url('administration:settings'),
            {'maintenance_mode': False},
            format='json',
        )

        self.client.force_authenticate(self.executive)
        response = self.client.get(self.url('administration:role-list'))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_message_is_the_configured_one(self):
        self.client.put(
            self.url('administration:settings'),
            {'maintenance_mode': True, 'maintenance_message': 'Back at 6pm.'},
            format='json',
        )
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.url('administration:role-list'))

        self.assertEqual(response.json()['detail'], 'Back at 6pm.')


# ----------------------------------------------------------------- audit logs


class AuditLogTests(AdminTestCase):
    @property
    def list_url(self):
        return self.url('administration:audit-list')

    def test_a_model_change_through_the_api_is_recorded(self):
        self.client.post(
            self.url('administration:employee-list'),
            {
                'employee_code': 'SFM-0400',
                'full_name': 'Audited Person',
                'email': 'audited@corp.com',
                'mobile': '+919876500400',
            },
            format='json',
        )

        entry = AuditLog.objects.filter(entity='accounts.User').latest('created_at')
        self.assertEqual(entry.action, AuditAction.CREATE)
        self.assertEqual(entry.actor, self.admin)
        self.assertIsNotNone(entry.ip_address)

    def test_changes_in_other_modules_are_recorded_too(self):
        """The receivers are on the models, so nine modules gained a trail
        without a line changing in any of them."""
        self.client.force_authenticate(self.executive)
        self.client.post(
            reverse('customers:list', kwargs={'version': 'v1'}),
            {
                'name': 'Audited Traders', 'contact_person': 'Ravi',
                'phone': '9811100011', 'type': CustomerType.RETAILER,
                'city': 'Noida', 'state': 'Uttar Pradesh', 'pincode': '201301',
            },
            format='json',
        )

        self.assertTrue(
            AuditLog.objects.filter(entity='customers.Customer').exists()
        )

    def test_a_fixture_built_outside_a_request_is_not_audited(self):
        """No actor and no address is not a trail entry worth having."""
        before = AuditLog.objects.count()

        Customer.objects.create(
            name='Quiet Shop', contact_person='Nobody', phone='9899900011',
            type=CustomerType.RETAILER, city='Noida',
            state='Uttar Pradesh', pincode='201301',
        )

        self.assertEqual(AuditLog.objects.count(), before)

    def test_a_sign_in_is_recorded(self):
        self.client.force_authenticate(None)

        self.client.post(
            reverse('accounts:login', kwargs={'version': 'v1'}),
            {'identifier': 'SFM-0000', 'password': PASSWORD},
            format='json',
        )

        entry = AuditLog.objects.filter(action=AuditAction.LOGIN).latest('created_at')
        self.assertIn('SFM-0000', entry.summary)

    def test_a_failed_sign_in_is_recorded(self):
        self.client.force_authenticate(None)

        self.client.post(
            reverse('accounts:login', kwargs={'version': 'v1'}),
            {'identifier': 'SFM-0000', 'password': 'wrong-password'},
            format='json',
        )

        entry = AuditLog.objects.filter(
            action=AuditAction.LOGIN_FAILED
        ).latest('created_at')
        self.assertIn('SFM-0000', entry.summary)

    def test_a_password_never_reaches_the_trail(self):
        self.client.force_authenticate(None)

        self.client.post(
            reverse('accounts:login', kwargs={'version': 'v1'}),
            {'identifier': 'SFM-0000', 'password': 'hunter2'},
            format='json',
        )

        for entry in AuditLog.objects.all():
            self.assertNotIn('hunter2', str(entry.changes))

    def test_the_trail_can_be_filtered(self):
        self.client.post(
            self.url('administration:employee-list'),
            {
                'employee_code': 'SFM-0500', 'full_name': 'Filter Me',
                'email': 'filter@corp.com', 'mobile': '+919876500500',
            },
            format='json',
        )

        by_action = self.client.get(self.list_url, {'action': AuditAction.CREATE})
        by_entity = self.client.get(self.list_url, {'entity': 'accounts.User'})

        self.assertGreaterEqual(by_action.data['count'], 1)
        self.assertGreaterEqual(by_entity.data['count'], 1)

    def test_the_trail_can_be_searched(self):
        self.client.post(
            self.url('administration:employee-list'),
            {
                'employee_code': 'SFM-0600', 'full_name': 'Searchable Person',
                'email': 'search@corp.com', 'mobile': '+919876500600',
            },
            format='json',
        )

        response = self.client.get(self.list_url, {'search': 'Searchable'})

        self.assertGreaterEqual(response.data['count'], 1)

    def test_one_entry_can_be_read(self):
        self.client.put(
            self.url('administration:settings'),
            {'app_version': '3.0.0'},
            format='json',
        )
        entry = AuditLog.objects.latest('created_at')

        response = self.client.get(
            self.url('administration:audit-detail', pk=entry.pk)
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(str(response.data['id']), str(entry.pk))

    def test_the_trail_is_paginated(self):
        response = self.client.get(self.list_url)

        self.assertIn('results', response.data)

    def test_the_trail_is_read_only(self):
        for method in (self.client.post, self.client.put, self.client.delete):
            with self.subTest(method=method.__name__):
                response = method(self.list_url, {}, format='json')
                self.assertEqual(
                    response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED
                )

    def test_an_executive_cannot_read_the_trail(self):
        self.client.force_authenticate(self.executive)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class RollbackTests(AdminTestCase):
    def test_a_failed_employee_create_leaves_nothing_behind(self):
        before = User.objects.count()

        response = self.client.post(
            self.url('administration:employee-list'),
            {
                'employee_code': 'SFM-0700',
                'full_name': 'Half Written',
                'email': 'half@corp.com',
                'mobile': '+919876500700',
                # A territory that does not exist: the create fails after the
                # user row would otherwise have been written.
                'primary_territory': '019fd0f4-0000-0000-0000-000000000000',
            },
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), before)

    def test_a_failed_approval_does_not_create_a_half_account(self):
        invite = InviteRequest.objects.create(
            full_name='Rolled Back', employee_code='SFM-0002',
            email='rolled@corp.com',
        )
        before = User.objects.count()

        response = self.client.post(
            self.url('administration:invite-approve', pk=invite.pk),
            {},
            format='json',
        )

        invite.refresh_from_db()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(User.objects.count(), before)
        self.assertTrue(invite.is_pending)


class QueryCountTests(AdminTestCase):
    def test_the_employee_list_does_not_grow_a_query_per_row(self):
        url = self.url('administration:employee-list')
        self.client.get(url)

        with self.assertNumQueries(4):
            self.client.get(url)

        for index in range(10):
            User.objects.create_user(
                f'sfm-9{index:03d}', f'Person {index}',
                email=f'p{index}@corp.com', mobile=f'+91987650{index:04d}',
                password=PASSWORD, status=User.Status.ACTIVE,
                role=self.exec_role, department=self.department,
            )

        with self.assertNumQueries(4):
            self.client.get(url)
