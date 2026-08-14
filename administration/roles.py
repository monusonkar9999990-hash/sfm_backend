"""The default role catalogue.

Four roles, defined by what they may do rather than by where they sit. The
codenames are the ones `accounts.BusinessPermission` registers — this file
groups them, it does not invent them.

Note on the demo data: `seed_demo` creates three roles of its own
(`administrator`, `area_manager`, `field_exec`) for the sample database. Those
predate this catalogue and are left alone; `ensure_default_roles` only creates
what is missing, so running it against a seeded database adds the four below
without disturbing the three already there.
"""

from django.contrib.auth.models import Permission

from accounts.models import Role

# Everything a Super Admin holds — i.e. all of them. Kept as a marker rather
# than a copied list, so a permission added later is included without an edit
# here being remembered.
ALL_PERMISSIONS = '__all__'

DEFAULT_ROLES = [
    {
        'code': 'super_admin',
        'name': 'Super Admin',
        'description': 'Unrestricted. Holds every permission, including future ones.',
        'permissions': ALL_PERMISSIONS,
    },
    {
        'code': 'admin',
        'name': 'Admin',
        'description': (
            'Runs the application: users, roles, master data, configuration '
            'and the audit trail.'
        ),
        'permissions': [
            'manage_users',
            'manage_roles',
            'edit_master_data',
            'edit_configuration',
            'approve_registrations',
            'view_audit_logs',
            'view_reports',
            'view_team_reports',
            'export_data',
            'view_pricing',
            'onboard_customers',
        ],
    },
    {
        'code': 'manager',
        'name': 'Manager',
        'description': (
            "Runs a team: approves exceptions, cancels orders, reads the "
            "team's figures. Cannot change users, roles or configuration."
        ),
        'permissions': [
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
    },
    {
        'code': 'sales_executive',
        'name': 'Sales Executive',
        'description': (
            'Works a beat: punches in, logs visits, onboards customers and '
            'books orders. Sees their own figures only.'
        ),
        'permissions': [
            'mark_attendance',
            'log_site_visits',
            'plan_beats',
            'onboard_customers',
            'place_orders',
            'view_pricing',
            'view_reports',
        ],
    },
]


def business_permissions():
    """The SFM permissions, which live under the `accounts` app label."""
    return Permission.objects.filter(
        content_type__app_label='accounts',
        content_type__model='businesspermission',
    )


def ensure_default_roles():
    """Creates any missing default role and sets its permissions.

    Idempotent, and safe to run against a database that already has roles: it
    matches on `code`, creates what is absent, and re-applies the permission
    set so a role that drifted comes back into line. It never deletes a role
    and never touches one whose code is not in the catalogue.
    """
    available = {p.codename: p for p in business_permissions()}
    results = []

    for definition in DEFAULT_ROLES:
        role, created = Role.objects.get_or_create(
            code=definition['code'],
            defaults={
                'name': definition['name'],
                'description': definition['description'],
                'is_system': True,
            },
        )

        wanted = definition['permissions']
        if wanted == ALL_PERMISSIONS:
            permissions = list(available.values())
        else:
            permissions = [available[name] for name in wanted if name in available]

        role.group.permissions.set(permissions)
        results.append((role, created, len(permissions)))

    return results
