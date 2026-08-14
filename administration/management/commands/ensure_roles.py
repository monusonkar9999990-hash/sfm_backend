"""Creates the default roles, or brings existing ones back into line.

    python manage.py ensure_roles

Safe to run repeatedly and safe to run on a live database — it creates what is
missing and re-applies the permission set for the four default roles. It never
deletes a role, and never touches one whose code is not in the catalogue.
"""

from django.core.management.base import BaseCommand

from administration.roles import ensure_default_roles


class Command(BaseCommand):
    help = 'Create or refresh the default roles (Super Admin, Admin, Manager, Sales Executive).'

    def handle(self, *args, **options):
        for role, created, permission_count in ensure_default_roles():
            label = 'created' if created else 'refreshed'
            self.stdout.write(
                f'  {label:<10} {role.name:<18} {permission_count} permissions'
            )

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('Default roles are in place.'))
