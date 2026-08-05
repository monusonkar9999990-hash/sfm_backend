"""Fill an empty database with enough to sign in and see something.

Development only. The schema exists but nothing is in it, so every endpoint
answers correctly and emptily — which makes it impossible to tell a working
client from a broken one. This gives the app a real user, a real role with
real permissions, and a beat with outlets on it.

Idempotent: run it as often as you like. It creates what is missing and
leaves what is there alone.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from accounts.models import (
    BusinessPermission,
    Department,
    Designation,
    Role,
    Territory,
    UserTerritory,
)
from attendance.models import GeoFence
from beats.models import Beat, BeatOutlet
from sitevisits.models import Site

User = get_user_model()

DEMO_PASSWORD = 'Demo@12345'

ROLES = {
    'field_exec': (
        'Field Sales Executive',
        'Works a beat, books orders, logs visits.',
        [
            'mark_attendance',
            'log_site_visits',
            'plan_beats',
            'onboard_customers',
            'place_orders',
            'view_pricing',
            'view_reports',
        ],
    ),
    'area_manager': (
        'Area Sales Manager',
        'Runs a team, approves exceptions, sees team figures.',
        [
            'mark_attendance',
            'log_site_visits',
            'plan_beats',
            'onboard_customers',
            'place_orders',
            'approve_discount',
            'cancel_orders',
            'view_pricing',
            'view_reports',
            'view_team_reports',
            'export_data',
        ],
    ),
    'administrator': (
        'Administrator',
        'Full access, including users and configuration.',
        [code for code, _ in BusinessPermission._meta.permissions],
    ),
}


class Command(BaseCommand):
    help = 'Creates demo roles, master data, users and a beat.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--password',
            default=DEMO_PASSWORD,
            help=f'Password for the demo accounts (default: {DEMO_PASSWORD}).',
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            # Seed accounts have a published password. They have no business
            # anywhere that is not somebody's laptop.
            raise CommandError(
                'seed_demo refuses to run with DEBUG off — it creates accounts '
                'with a known password.'
            )

        password = options['password']

        roles = self._roles()
        departments = self._departments()
        designations = self._designations()
        territories = self._territories()

        manager = self._user(
            'SFM-0001',
            'Rahul Deshpande',
            'rahul.d@example.com',
            '+919876500022',
            password,
            role=roles['area_manager'],
            department=departments['SLS'],
            designation=designations['ASM'],
            territory=territories['NZ-DEL'],
            is_staff=True,
        )
        self._user(
            'SFM-0002',
            'Alex Mercer',
            'demo@salesforce.com',
            '+919876543210',
            password,
            role=roles['field_exec'],
            department=departments['SLS'],
            designation=designations['FSE'],
            territory=territories['DEL'],
            manager=manager,
        )
        admin = self._user(
            'SFM-0000',
            'Priya Nair',
            'priya.nair@example.com',
            '+919876500011',
            password,
            role=roles['administrator'],
            department=departments['HO'],
            designation=designations['ADM'],
            territory=territories['NZ-DEL'],
            is_staff=True,
            is_superuser=True,
        )

        self._geofence(territories['NZ-DEL'])
        self._sites(territories['DEL'])
        self._beat(assigned_to=User.objects.get(employee_code='SFM-0002'),
                   territory=territories['DEL'])

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Seeded. Sign in with any of:'))
        for code, name in (
            ('SFM-0002', 'field executive'),
            ('SFM-0001', 'area manager'),
            ('SFM-0000', f'administrator, Django admin at /admin/'),
        ):
            self.stdout.write(f'  {code}  ({name})')
        self.stdout.write(f'  password: {password}')
        self.stdout.write('')
        self.stdout.write(
            'The login endpoint also accepts the email or the mobile number.'
        )
        # Referenced so the linter keeps the binding meaningful.
        assert admin.is_superuser

    # ------------------------------------------------------------------ parts

    def _roles(self):
        roles = {}
        for code, (name, description, codenames) in ROLES.items():
            role, created = Role.objects.get_or_create(
                code=code,
                defaults={
                    'name': name,
                    'description': description,
                    'is_system': True,
                },
            )
            role.group.permissions.set(
                Permission.objects.filter(
                    content_type__app_label='accounts', codename__in=codenames
                )
            )
            roles[code] = role
            self._say('role', name, created)
        return roles

    def _departments(self):
        rows = {'SLS': 'Sales', 'HO': 'Head office'}
        out = {}
        for code, name in rows.items():
            obj, created = Department.objects.get_or_create(
                code=code, defaults={'name': name}
            )
            out[code] = obj
            self._say('department', name, created)
        return out

    def _designations(self):
        rows = {'FSE': ('Field Sales Executive', 1),
                'ASM': ('Area Sales Manager', 3),
                'ADM': ('Administrator', 5)}
        out = {}
        for code, (name, level) in rows.items():
            obj, created = Designation.objects.get_or_create(
                code=code, defaults={'name': name, 'level': level}
            )
            out[code] = obj
            self._say('designation', name, created)
        return out

    def _territories(self):
        zone, created = Territory.objects.get_or_create(
            code='NZ-DEL',
            defaults={'name': 'North Zone', 'kind': Territory.Kind.ZONE},
        )
        self._say('territory', zone.name, created)

        city, created = Territory.objects.get_or_create(
            code='DEL',
            defaults={
                'name': 'Delhi NCR',
                'kind': Territory.Kind.CITY,
                'parent': zone,
            },
        )
        self._say('territory', city.name, created)
        return {'NZ-DEL': zone, 'DEL': city}

    def _user(
        self,
        code,
        name,
        email,
        mobile,
        password,
        *,
        role,
        department,
        designation,
        territory,
        manager=None,
        is_staff=False,
        is_superuser=False,
    ):
        user = User.objects.filter(employee_code=code).first()
        if user is None:
            user = User.objects.create_user(
                employee_code=code,
                full_name=name,
                email=email,
                mobile=mobile,
                password=password,
                status=User.Status.ACTIVE,
                # Seed accounts are meant to be signed into straight away.
                must_change_password=False,
                is_staff=is_staff,
                is_superuser=is_superuser,
                date_joined=timezone.localdate(),
            )
            self._say('user', f'{code} {name}', True)
        else:
            self._say('user', f'{code} {name}', False)

        user.role = role
        user.department = department
        user.designation = designation
        user.reporting_manager = manager
        user.save()

        UserTerritory.objects.get_or_create(
            user=user, territory=territory, defaults={'is_primary': True}
        )
        return user

    def _sites(self, territory):
        rows = [
            ('SITE-01', 'Green Valley Apartments', 'cus_1',
             'Shree Balaji Traders', 'Plot 14, Sector 62', 'Noida',
             'structure', '28.612900', '77.229500'),
            ('SITE-02', 'Riverfront Villas', 'cus_2', 'Verma Hardware',
             '8, Yamuna Bank Road', 'New Delhi', 'foundation',
             '28.656200', '77.241000'),
            # Deliberately unplotted: a site added from the office before
            # anyone has stood on it, which the check-in has to cope with.
            ('SITE-03', 'Sunrise Row Houses', 'cus_3',
             'Gupta Building Material', '22, Pusa Road', 'New Delhi',
             'brickwork', None, None),
        ]
        for code, name, ref, customer, address, city, stage, lat, lng in rows:
            site, created = Site.objects.get_or_create(
                code=code,
                defaults={
                    'name': name,
                    'customer_ref': ref,
                    'customer_name': customer,
                    'address': address,
                    'city': city,
                    'stage': stage,
                    'latitude': lat,
                    'longitude': lng,
                    'territory': territory,
                },
            )
            self._say('site', site.name, created)

    def _geofence(self, territory):
        fence, created = GeoFence.objects.get_or_create(
            code='HO',
            defaults={
                'name': 'Head Office',
                # India Gate, New Delhi.
                'latitude': '28.612900',
                'longitude': '77.229500',
                'radius_meters': 300,
                'territory': territory,
            },
        )
        self._say('geofence', fence.name, created)

    def _beat(self, assigned_to, territory):
        beat, created = Beat.objects.get_or_create(
            code='DEL-N-01',
            defaults={
                'name': 'Karol Bagh North',
                'area': 'Karol Bagh',
                'city': 'New Delhi',
                'assigned_user': assigned_to,
                'territory': territory,
                # Monday and Thursday.
                'weekdays': [1, 4],
            },
        )
        self._say('beat', beat.name, created)

        outlets = [
            ('cus_1', 'Shree Balaji Traders', '14, Karol Bagh Market', '9811122233'),
            ('cus_2', 'Verma Hardware', 'Shop 8, Ajmal Khan Road', '9822233344'),
            ('cus_3', 'Gupta Building Material', '22, Pusa Road', '9833344455'),
        ]
        for index, (ref, name, address, phone) in enumerate(outlets, start=1):
            BeatOutlet.objects.get_or_create(
                beat=beat,
                customer_ref=ref,
                defaults={
                    'customer_name': name,
                    'address': address,
                    'phone': phone,
                    'sequence': index,
                },
            )

    def _say(self, kind, name, created):
        verb = 'created' if created else 'exists'
        style = self.style.SUCCESS if created else self.style.WARNING
        self.stdout.write(style(f'  {verb:8} {kind}: {name}'))
