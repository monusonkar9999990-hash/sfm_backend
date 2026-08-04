"""Object manager for the custom user model.

`create_user` deliberately leaves the password unusable when none is given:
that is the state an *invited* user sits in until they set one through the
invite flow, and it is different from a user whose password simply failed.
"""

from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    # Referenced by migrations, so it must stay importable and argument-free.
    use_in_migrations = True

    def _create_user(
        self,
        employee_code,
        full_name,
        email=None,
        mobile=None,
        password=None,
        **extra_fields,
    ):
        if not employee_code:
            raise ValueError('An employee code is required')
        if not full_name:
            raise ValueError('A full name is required')
        if not email and not mobile:
            raise ValueError(
                'A user needs an email address or a mobile number to sign in'
            )

        user = self.model(
            # Normalisation happens here and in Model.save(), because users are
            # also created by the admin and by serializers that never touch
            # this manager.
            employee_code=employee_code.strip().upper(),
            full_name=full_name.strip(),
            email=self.normalize_email(email).lower() if email else None,
            mobile=mobile.strip() if mobile else None,
            **extra_fields,
        )
        # set_password(None) stores an unusable hash — check_password() then
        # always fails, which is exactly right for an invited user.
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(
        self,
        employee_code,
        full_name,
        email=None,
        mobile=None,
        password=None,
        **extra_fields,
    ):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        extra_fields.setdefault('must_change_password', True)
        return self._create_user(
            employee_code, full_name, email, mobile, password, **extra_fields
        )

    def create_superuser(
        self,
        employee_code,
        full_name,
        email=None,
        mobile=None,
        password=None,
        **extra_fields,
    ):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        extra_fields.setdefault('is_active', True)
        # A superuser is created by someone at a keyboard who just typed the
        # password, so there is nothing to force a change of.
        extra_fields.setdefault('must_change_password', False)
        extra_fields.setdefault('status', self.model.Status.ACTIVE)

        if extra_fields['is_staff'] is not True:
            raise ValueError('A superuser must have is_staff=True')
        if extra_fields['is_superuser'] is not True:
            raise ValueError('A superuser must have is_superuser=True')

        return self._create_user(
            employee_code, full_name, email, mobile, password, **extra_fields
        )
