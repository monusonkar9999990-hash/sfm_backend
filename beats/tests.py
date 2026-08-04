"""End-to-end tests for the beat endpoints.

Run against a throwaway `test_sfm_db`, created and dropped by the test runner.
"""

import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import Role
from .models import (
    Beat,
    BeatOutlet,
    BeatPlan,
    BeatPlanStatus,
    BeatPlanVisit,
    VisitStatus,
    validate_weekdays,
)

User = get_user_model()

PASSWORD = 'Str0ng-Pass!7'


class BeatTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.role = Role.objects.create(name='Field Executive', code='field_exec')
        cls.role.group.permissions.set(
            Permission.objects.filter(
                content_type__app_label='accounts', codename='plan_beats'
            )
        )
        cls.user = User.objects.create_user(
            'sfm-0002',
            'Alex Mercer',
            email='alex@corp.com',
            mobile='+919876543210',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
        )
        cls.colleague = User.objects.create_user(
            'sfm-0003',
            'Sneha Kulkarni',
            email='sneha@corp.com',
            mobile='+919876500044',
            password=PASSWORD,
            status=User.Status.ACTIVE,
            role=cls.role,
        )
        # Same role withheld, to prove the permission class bites.
        cls.outsider = User.objects.create_user(
            'sfm-0004',
            'Vikram Rao',
            email='vikram@corp.com',
            mobile='+919876500055',
            password=PASSWORD,
            status=User.Status.ACTIVE,
        )

        cls.beat = Beat.objects.create(
            name='Karol Bagh North',
            code='DEL-N-01',
            area='Karol Bagh',
            city='New Delhi',
            assigned_user=cls.user,
            weekdays=[1, 4],
        )
        for index, name in enumerate(
            ['Shree Balaji Traders', 'Verma Hardware', 'Gupta Building Material'], start=1
        ):
            BeatOutlet.objects.create(
                beat=cls.beat,
                customer_ref=f'cus_{index}',
                customer_name=name,
                address=f'{index}, Karol Bagh Market',
                phone=f'98111222{index}{index}',
                sequence=index,
            )

    def setUp(self):
        cache.clear()
        self.authenticate(self.user)

    # ------------------------------------------------------------------ helpers

    def url(self, name, **kwargs):
        return reverse(f'beats:{name}', kwargs={'version': 'v1', **kwargs})

    def authenticate(self, user):
        token = self.client.post(
            reverse('accounts:login', kwargs={'version': 'v1'}),
            {'identifier': user.employee_code, 'password': PASSWORD},
            format='json',
        ).data['access']
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')

    def schedule(self, when=None, **overrides):
        payload = {'beat': str(self.beat.id), 'date': str(when or timezone.localdate())}
        payload.update(overrides)
        return self.client.post(self.url('plans'), payload, format='json')

    def make_plan(self):
        response = self.schedule()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return BeatPlan.objects.get(pk=response.data['id'])

    # ------------------------------------------------------------------- model

    def test_weekday_validation(self):
        for bad in ([], [0], [8], [1, 1], 'monday'):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError):
                    validate_weekdays(bad)
        validate_weekdays([1, 4])

    def test_schedule_label_reads_in_week_order(self):
        self.assertEqual(self.beat.schedule_label, 'Mon, Thu')
        self.assertTrue(self.beat.runs_on(timezone.localdate() - timedelta(days=timezone.localdate().isoweekday() - 1)))

    # -------------------------------------------------------------- beat listing

    def test_beat_list_shows_the_route_in_call_order(self):
        response = self.client.get(self.url('list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        beat = response.data['results'][0]
        self.assertEqual(beat['code'], 'DEL-N-01')
        self.assertEqual([o['sequence'] for o in beat['outlets']], [1, 2, 3])
        self.assertEqual(beat['outlets'][0]['customer_id'], 'cus_1')
        self.assertEqual(beat['weekdays'], [1, 4])

    def test_beat_list_hides_other_peoples_routes(self):
        self.authenticate(self.colleague)
        response = self.client.get(self.url('list'))
        self.assertEqual(response.data['count'], 0)

    def test_beat_list_requires_authentication(self):
        self.client.credentials()
        self.assertEqual(
            self.client.get(self.url('list')).status_code, status.HTTP_401_UNAUTHORIZED
        )

    # ------------------------------------------------------------- plan creation

    def test_scheduling_snapshots_the_route(self):
        response = self.schedule()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['planned_outlet_count'], 3)
        self.assertEqual(response.data['status'], BeatPlanStatus.PLANNED)
        self.assertEqual([v['sequence'] for v in response.data['visits']], [1, 2, 3])
        self.assertTrue(all(v['status'] == VisitStatus.PENDING for v in response.data['visits']))

    def test_a_later_route_change_does_not_touch_an_existing_plan(self):
        plan = self.make_plan()
        BeatOutlet.objects.create(
            beat=self.beat, customer_ref='cus_9', customer_name='New Shop', sequence=4
        )
        plan.refresh_from_db()
        self.assertEqual(plan.planned_outlet_count, 3)
        self.assertEqual(plan.visits.count(), 3)

    def test_the_same_beat_cannot_be_planned_twice_on_a_day(self):
        self.schedule()
        response = self.schedule()
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(BeatPlan.objects.count(), 1)

    def test_the_database_itself_refuses_a_duplicate_plan(self):
        self.make_plan()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                BeatPlan.objects.create(
                    beat=self.beat, user=self.user, date=timezone.localdate()
                )

    def test_a_past_date_is_refused(self):
        response = self.schedule(timezone.localdate() - timedelta(days=1))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('date', response.data)

    def test_an_inactive_beat_cannot_be_planned(self):
        self.beat.is_active = False
        self.beat.save(update_fields=['is_active'])
        response = self.schedule()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.beat.is_active = True
        self.beat.save(update_fields=['is_active'])

    def test_someone_elses_beat_cannot_be_planned(self):
        self.authenticate(self.colleague)
        response = self.schedule()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('beat', response.data)

    def test_planning_requires_the_plan_beats_permission(self):
        self.authenticate(self.outsider)
        self.assertEqual(self.schedule().status_code, status.HTTP_403_FORBIDDEN)

    def test_an_off_schedule_day_is_allowed_but_flagged(self):
        # The beat runs Mon and Thu; find the next day that is neither.
        day = timezone.localdate()
        while day.isoweekday() in (1, 4):
            day += timedelta(days=1)
        response = self.schedule(day)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['off_schedule'])

    def test_replaying_a_sync_id_returns_the_original_plan(self):
        sync_id = str(uuid.uuid4())
        first = self.schedule(sync_id=sync_id)
        replay = self.schedule(sync_id=sync_id)
        self.assertEqual(replay.status_code, status.HTTP_200_OK)
        self.assertEqual(replay.data['id'], first.data['id'])
        self.assertEqual(BeatPlan.objects.count(), 1)

    # ------------------------------------------------------------------- listing

    def test_plan_list_defaults_to_the_current_month(self):
        self.make_plan()
        response = self.client.get(self.url('plans'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)

    def test_plan_list_rejects_a_malformed_date(self):
        response = self.client.get(self.url('plans'), {'from': 'yesterday'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plan_detail_is_closed_to_other_users(self):
        plan = self.make_plan()
        self.authenticate(self.colleague)
        response = self.client.get(self.url('plan-detail', pk=plan.pk))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --------------------------------------------------------------- start beat

    def test_start_moves_the_plan_into_progress(self):
        plan = self.make_plan()
        response = self.client.post(self.url('plan-start', pk=plan.pk))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], BeatPlanStatus.IN_PROGRESS)
        self.assertIsNotNone(response.data['started_at'])

    def test_starting_twice_is_harmless(self):
        plan = self.make_plan()
        first = self.client.post(self.url('plan-start', pk=plan.pk))
        again = self.client.post(self.url('plan-start', pk=plan.pk))
        self.assertEqual(again.status_code, status.HTTP_200_OK)
        self.assertEqual(again.data['started_at'], first.data['started_at'])

    def test_start_is_refused_on_a_closed_plan(self):
        plan = self.make_plan()
        self.client.post(self.url('plan-complete', pk=plan.pk), {}, format='json')
        response = self.client.post(self.url('plan-start', pk=plan.pk))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_start_is_closed_to_other_users(self):
        plan = self.make_plan()
        self.authenticate(self.colleague)
        response = self.client.post(self.url('plan-start', pk=plan.pk))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # -------------------------------------------------------------- visit status

    def test_marking_a_visit_covers_the_outlet_and_starts_the_run(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], BeatPlanStatus.IN_PROGRESS)
        self.assertEqual(response.data['covered_customer_ids'], ['cus_1'])
        self.assertEqual(response.data['covered_count'], 1)
        self.assertAlmostEqual(response.data['coverage'], 1 / 3, places=4)

    def test_visiting_every_outlet_closes_the_plan(self):
        plan = self.make_plan()
        for visit in plan.visits.all():
            response = self.client.post(
                self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json'
            )
        self.assertEqual(response.data['status'], BeatPlanStatus.COMPLETED)
        self.assertTrue(response.data['is_fully_covered'])
        self.assertIsNotNone(response.data['closed_at'])

    def test_a_visit_cannot_be_marked_twice(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        self.client.post(self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json')
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_visit_stamped_in_the_future_is_refused(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk),
            {'visited_at': (timezone.now() + timedelta(hours=2)).isoformat()},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_visit_from_another_plan_is_not_found(self):
        plan = self.make_plan()
        other_plan = BeatPlan.objects.create(
            beat=self.beat, user=self.user, date=timezone.localdate() + timedelta(days=1)
        )
        stray = BeatPlanVisit.objects.create(
            plan=other_plan, customer_ref='cus_1', customer_name='Elsewhere'
        )
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=stray.pk), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    # ---------------------------------------------------------------- skip visit

    def test_skipping_records_the_reason_and_never_counts_as_covered(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        response = self.client.post(
            self.url('visit-skip', pk=plan.pk, visit_pk=visit.pk),
            {'reason': 'Shop shut for a wedding'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['covered_customer_ids'], [])
        self.assertEqual(response.data['skipped_count'], 1)
        self.assertEqual(response.data['visits'][0]['skip_reason'], 'Shop shut for a wedding')

    def test_skipping_without_a_reason_is_refused(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        for body in ({}, {'reason': '   '}):
            with self.subTest(body=body):
                response = self.client.post(
                    self.url('visit-skip', pk=plan.pk, visit_pk=visit.pk),
                    body,
                    format='json',
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_skipped_visit_cannot_then_be_marked_visited(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        self.client.post(
            self.url('visit-skip', pk=plan.pk, visit_pk=visit.pk),
            {'reason': 'Closed'},
            format='json',
        )
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_skipping_every_outlet_leaves_the_plan_open(self):
        # Nothing was covered, so the run is not complete — it is a missed day
        # waiting to be closed.
        plan = self.make_plan()
        for visit in plan.visits.all():
            response = self.client.post(
                self.url('visit-skip', pk=plan.pk, visit_pk=visit.pk),
                {'reason': 'Market bandh'},
                format='json',
            )
        self.assertEqual(response.data['status'], BeatPlanStatus.IN_PROGRESS)

    # ------------------------------------------------------------- complete beat

    def test_completing_with_coverage_marks_it_completed(self):
        plan = self.make_plan()
        visit = plan.visits.first()
        self.client.post(self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json')

        response = self.client.post(
            self.url('plan-complete', pk=plan.pk), {'remarks': 'Two shops shut'}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], BeatPlanStatus.COMPLETED)
        self.assertEqual(response.data['remarks'], 'Two shops shut')
        self.assertIsNotNone(response.data['closed_at'])

    def test_completing_with_nothing_covered_marks_it_missed(self):
        plan = self.make_plan()
        response = self.client.post(self.url('plan-complete', pk=plan.pk), {}, format='json')
        self.assertEqual(response.data['status'], BeatPlanStatus.MISSED)

    def test_completing_twice_is_refused(self):
        plan = self.make_plan()
        self.client.post(self.url('plan-complete', pk=plan.pk), {}, format='json')
        response = self.client.post(self.url('plan-complete', pk=plan.pk), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_a_closed_plan_refuses_further_visits(self):
        plan = self.make_plan()
        self.client.post(self.url('plan-complete', pk=plan.pk), {}, format='json')
        visit = plan.visits.first()
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk), {}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_complete_requires_the_permission(self):
        plan = self.make_plan()
        self.authenticate(self.outsider)
        response = self.client.post(self.url('plan-complete', pk=plan.pk), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
