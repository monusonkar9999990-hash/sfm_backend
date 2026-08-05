"""Authentication endpoints.

Sign in, refresh, read the current profile, change a password, sign out.
Nothing here creates or edits a user — user administration is a separate
concern and lives behind the admin permissions.
"""

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.generics import GenericAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import (
    ChangePasswordSerializer,
    InviteRequestSerializer,
    LoginSerializer,
    LogoutSerializer,
    UserSerializer,
)

User = get_user_model()


class LoginView(TokenObtainPairView):
    """Sign in with a mobile number, email address or employee code.

    Accepts any of the three as `identifier`; the authentication backend works
    out which one it is. Returns an access/refresh pair together with the full
    user profile, so the client needs one round trip rather than two.

    **Request**  `{"identifier": "9876543210", "password": "…"}`

    **Responses**
    * `200` — `{access, refresh, user, must_change_password}`
    * `400` — a field is missing
    * `401` — no active account matches those credentials
    * `429` — too many attempts (10 per minute per client)
    """

    serializer_class = LoginSerializer
    permission_classes = [AllowAny]
    # Rate defined in settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['login'].
    throttle_scope = 'login'


class RefreshTokenView(TokenRefreshView):
    """Exchange a refresh token for a new access token.

    Refresh rotation is on, so the response also carries a **new** refresh
    token and the one just used is blacklisted. Clients must store the new
    refresh token; replaying the old one fails.

    **Request**  `{"refresh": "…"}`

    **Responses**
    * `200` — `{access, refresh}`
    * `401` — the token is expired, malformed or already blacklisted
    """

    permission_classes = [AllowAny]


class CurrentUserView(RetrieveAPIView):
    """Return the signed-in user's profile.

    Includes the permission keys the client uses to hide or show features, so
    a revoked permission takes effect on the next call rather than when the
    access token expires.

    **Responses**
    * `200` — the user object
    * `401` — missing or invalid access token
    """

    serializer_class = UserSerializer
    permission_classes = [IsAuthenticated]

    def get_object(self):
        # request.user is already loaded, but re-fetching with the related rows
        # turns several lazy queries during serialisation into one.
        return (
            User.objects.select_related(
                'role', 'role__group', 'department', 'designation', 'reporting_manager'
            )
            .prefetch_related('territory_links__territory')
            .get(pk=self.request.user.pk)
        )


class ChangePasswordView(GenericAPIView):
    """Change the signed-in user's password.

    Every refresh token the user holds is blacklisted and a fresh pair is
    issued, so a password change signs every other device out — which is the
    point of changing it. The client should replace its stored tokens with the
    ones in the response.

    **Request**
    `{"current_password": "…", "new_password": "…", "confirm_password": "…"}`

    **Responses**
    * `200` — `{detail, access, refresh}`
    * `400` — wrong current password, mismatch, or the new password fails a validator
    * `401` — missing or invalid access token
    """

    serializer_class = ChangePasswordSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        user.set_password(serializer.validated_data['new_password'])
        user.must_change_password = False
        user.save(update_fields=['password', 'must_change_password'])

        # A changed password must not leave old sessions alive. JWTs are
        # stateless, so the blacklist is the only way to end them.
        for outstanding in OutstandingToken.objects.filter(user=user):
            BlacklistedToken.objects.get_or_create(token=outstanding)

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                'detail': 'Password changed. Other devices have been signed out.',
                'access': str(refresh.access_token),
                'refresh': str(refresh),
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(GenericAPIView):
    """Sign out by blacklisting the caller's refresh token.

    The access token is not revoked — it is stateless and short-lived and
    expires on its own. The refresh token is what keeps a session alive, so
    that is what gets killed.

    **Request**  `{"refresh": "…"}`

    **Responses**
    * `205` — signed out, the client should clear its stored tokens
    * `400` — the token is invalid, already blacklisted, or belongs to someone else
    * `401` — missing or invalid access token
    """

    serializer_class = LogoutSerializer
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_205_RESET_CONTENT)


class RequestInviteView(GenericAPIView):
    """Ask an administrator for an account.

    This product is invite-only: nobody signs themselves up. The endpoint
    records a request and grants nothing — no account, no token, no hint that
    the request went anywhere except into a queue.

    The reply is identical whether the request was recorded, the person
    already has an account, or they already have a request waiting. An
    endpoint open to the internet that distinguishes those cases is a
    directory of who works here.

    **Request**
    `{"full_name": "…", "employee_code": "SFM-0142", "email": "…",
      "mobile": "+919876543210", "message": "…"}`

    **Responses**
    * `202` — the request was received (whatever happened behind it)
    * `400` — a field is missing or malformed
    * `429` — too many requests from this client
    """

    serializer_class = InviteRequestSerializer
    permission_classes = [AllowAny]
    throttle_scope = 'invite'

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.record()

        return Response(
            {
                'detail': (
                    'Thanks — your request has been sent to an administrator. '
                    'You will hear from them once it has been reviewed.'
                )
            },
            status=status.HTTP_202_ACCEPTED,
        )
