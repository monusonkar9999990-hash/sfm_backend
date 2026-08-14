"""The one endpoint this app publishes: a ticket onto the socket."""

from rest_framework import serializers, status
from rest_framework.generics import GenericAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from reports.permissions import CanViewReports

from .server import TICKET_TTL_SECONDS, issue_ticket


class TicketSerializer(serializers.Serializer):
    ticket = serializers.CharField()
    expires_in = serializers.IntegerField()
    scope = serializers.ChoiceField(choices=['team', 'self'])


class RealtimeTicketView(GenericAPIView):
    """A one-shot ticket for opening a live-updates socket.

    Asked for by the portal's *server*, which holds the access token, and
    handed to the browser, which holds nothing. That is the whole point: the
    portal keeps its tokens in `httpOnly` cookies, and a socket authenticated
    with a bearer token would mean handing that token to JavaScript first.

    The ticket is spent on the first connect and expires in a minute either
    way. It opens the notification socket and nothing else — no API call
    accepts it.

    Gated on `view_reports`, the same permission as every report: somebody who
    may not read the figures has no reason to be told they changed.

    **Responses**
    * `200` — `{ticket, expires_in, scope}`
    * `401` — missing or invalid access token
    * `403` — the role does not include reports
    """

    serializer_class = TicketSerializer
    permission_classes = [IsAuthenticated, CanViewReports]
    http_method_names = ['post', 'options']

    def post(self, request, *args, **kwargs):
        payload = {
            'ticket': issue_ticket(request.user),
            'expires_in': TICKET_TTL_SECONDS,
            'scope': (
                'team'
                if request.user.has_perm('accounts.view_team_reports')
                else 'self'
            ),
        }
        return Response(self.get_serializer(payload).data, status=status.HTTP_200_OK)
