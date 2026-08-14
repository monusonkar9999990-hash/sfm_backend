"""Tests for the live-updates socket.

The socket handshake itself is python-socketio's job and is tested there. What
belongs here is the part this project decided: who may get a ticket, that a
ticket is worth exactly one use, that an event names an entity and carries no
business data, and — the one that matters most — that a failure to notify can
never fail the write that triggered it.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from attendance.models import Attendance
from realtime import server

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class RealtimeTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        def role(name, code, codenames):
            r = Role.objects.create(name=name, code=code)
            r.group.permissions.set(
                Permission.objects.filter(
                    content_type__app_label='accounts', codename__in=codenames
                )
            )
            return r

        cls.exec_role = role('Field Executive', 'rt_exec', ['view_reports'])
        cls.manager_role = role(
            'Area Manager', 'rt_manager', ['view_reports', 'view_team_reports']
        )

        cls.executive = User.objects.create_user(
            'rt-0002', 'Alex Mercer', email='rt-alex@corp.com',
            mobile='+919876543230', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.exec_role,
        )
        cls.manager = User.objects.create_user(
            'rt-0001', 'Rahul Deshpande', email='rt-rahul@corp.com',
            mobile='+919876543231', password=PASSWORD,
            status=User.Status.ACTIVE, role=cls.manager_role,
        )
        cls.outsider = User.objects.create_user(
            'rt-0003', 'Vikram Rao', email='rt-vikram@corp.com',
            mobile='+919876543232', password=PASSWORD,
            status=User.Status.ACTIVE,
        )

    def setUp(self):
        cache.clear()

    @property
    def ticket_url(self):
        return reverse('realtime:ticket', kwargs={'version': 'v1'})


class TicketTests(RealtimeTestCase):
    def test_a_manager_gets_a_ticket_scoped_to_the_team(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(self.ticket_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['scope'], 'team')
        self.assertTrue(response.data['ticket'])
        self.assertEqual(response.data['expires_in'], server.TICKET_TTL_SECONDS)

    def test_an_executive_gets_a_ticket_scoped_to_themselves(self):
        self.client.force_authenticate(self.executive)

        self.assertEqual(self.client.post(self.ticket_url).data['scope'], 'self')

    def test_somebody_who_may_not_read_reports_gets_no_ticket(self):
        # Being told the figures changed is a smaller thing than reading them,
        # but it is still a thing about work this person may not see.
        self.client.force_authenticate(self.outsider)

        self.assertEqual(
            self.client.post(self.ticket_url).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_it_needs_a_signed_in_caller(self):
        self.assertEqual(
            self.client.post(self.ticket_url).status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_a_ticket_is_spent_on_first_use(self):
        self.client.force_authenticate(self.manager)
        ticket = self.client.post(self.ticket_url).data['ticket']

        first = server._spend_ticket(ticket)
        second = server._spend_ticket(ticket)

        self.assertEqual(first['user_id'], str(self.manager.pk))
        self.assertTrue(first['team'])
        self.assertIsNone(second)

    def test_an_unknown_ticket_buys_nothing(self):
        self.assertIsNone(server._spend_ticket('not-a-real-ticket'))
        self.assertIsNone(server._spend_ticket(''))
        self.assertIsNone(server._spend_ticket(None))


class NotificationTests(RealtimeTestCase):
    """What goes out when a record moves."""

    def punch(self, user):
        return Attendance.objects.create(
            user=user,
            day=timezone.localdate(),
            punch_in_at=timezone.now() - timedelta(hours=1),
            punch_in_latitude=Decimal('28.612900'),
            punch_in_longitude=Decimal('77.229500'),
        )

    def test_writing_a_record_tells_the_team_room_and_the_owner(self):
        with patch.object(server.sio, 'emit') as emit:
            self.punch(self.executive)

        rooms = [call.kwargs['room'] for call in emit.call_args_list]
        self.assertIn(server.TEAM_ROOM, rooms)
        self.assertIn(f'user:{self.executive.pk}', rooms)

    def test_the_event_names_the_entity_and_carries_nothing_else(self):
        # The whole design rests on this: the socket is a doorbell, not a
        # delivery. Anything more would be a second path to business data with
        # a second set of permission checks to get wrong.
        with patch.object(server.sio, 'emit') as emit:
            self.punch(self.executive)

        event, payload = emit.call_args_list[0].args
        self.assertEqual(event, 'changed')
        self.assertEqual(set(payload), {'entity', 'action'})
        self.assertEqual(payload['entity'], 'attendance')
        self.assertEqual(payload['action'], 'created')

    def test_an_update_is_reported_as_an_update(self):
        record = self.punch(self.executive)

        with patch.object(server.sio, 'emit') as emit:
            record.punch_out_at = timezone.now()
            record.save(update_fields=['punch_out_at'])

        self.assertEqual(emit.call_args_list[0].args[1]['action'], 'updated')

    def test_a_broken_socket_never_breaks_the_write(self):
        # The one that matters. A punch made at a shop door must not fail
        # because a dashboard nobody is watching could not be told about it.
        with patch.object(server.sio, 'emit', side_effect=RuntimeError('down')):
            record = self.punch(self.executive)

        self.assertTrue(Attendance.objects.filter(pk=record.pk).exists())

    def test_notifications_can_be_switched_off(self):
        with self.settings(REALTIME_ENABLED=False):
            with patch.object(server.sio, 'emit') as emit:
                self.punch(self.executive)

        emit.assert_not_called()
