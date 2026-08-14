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

    def test_a_phone_a_minute_ahead_of_the_server_is_still_accepted(self):
        # Two machines nobody is synchronising drift apart, and the rep holding
        # the phone cannot do anything about it. The check is meant to catch a
        # clock that is wrong by hours — it used to refuse one that was wrong
        # by seconds, which made a correct phone unable to work a beat.
        plan = self.make_plan()
        visit = plan.visits.first()
        response = self.client.post(
            self.url('visit-mark', pk=plan.pk, visit_pk=visit.pk),
            {'visited_at': (timezone.now() + timedelta(minutes=2)).isoformat()},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

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

    # ------------------------------------------------------------------- bulk
    #
    # Multi-select on the planning screen. Doing it as N requests leaves a
    # half-applied selection when the third fails, with nothing on the client
    # able to say which two landed.

    def second_beat(self, code='DEL-N-99', **overrides):
        """Another route of this user, so a selection has something to select."""
        beat = Beat.objects.create(
            code=code,
            name=overrides.pop('name', 'Second route'),
            area='B',
            city='Delhi',
            assigned_user=overrides.pop('assigned_user', self.user),
            weekdays=[1, 2, 3, 4, 5, 6, 7],
            **overrides,
        )
        BeatOutlet.objects.create(
            beat=beat, customer_ref='cus-9', customer_name='Ninth shop', sequence=1
        )
        return beat

    def bulk(self, beats, when=None, **extra):
        payload = {
            'beats': [str(b.pk) for b in beats],
            'date': str(when or timezone.localdate() + timedelta(days=1)),
        }
        payload.update(extra)
        return self.client.post(self.url('plans-bulk'), payload, format='json')

    def test_bulk_plans_several_beats_in_one_call(self):
        other = self.second_beat()

        response = self.bulk([self.beat, other])

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['requested'], 2)
        self.assertEqual(response.data['applied'], 2)
        self.assertEqual(response.data['failed'], 0)
        self.assertEqual(BeatPlan.objects.filter(user=self.user).count(), 2)

    def test_bulk_snapshots_outlets_the_same_way_a_single_plan_does(self):
        other = self.second_beat()

        self.bulk([self.beat, other])

        # A bulk call that skipped the snapshot would create plans with no
        # stops on them, which is worse than not creating them at all.
        for plan in BeatPlan.objects.all():
            self.assertEqual(plan.visits.count(), plan.planned_outlet_count)
            self.assertGreater(plan.planned_outlet_count, 0)

    def test_bulk_one_bad_beat_does_not_abandon_the_rest(self):
        inactive = self.second_beat(code='DEL-N-98', name='Retired', is_active=False)

        response = self.bulk([self.beat, inactive])

        self.assertEqual(response.data['applied'], 1)
        self.assertEqual(response.data['failed'], 1)

        rejected = [r for r in response.data['results'] if r['status'] == 'rejected']
        # The single-plan serializer message, not a second copy of the rule.
        self.assertIn('inactive', rejected[0]['detail'].lower())

    def test_bulk_a_beat_already_planned_is_skipped_not_failed(self):
        other = self.second_beat()
        tomorrow = timezone.localdate() + timedelta(days=1)
        self.bulk([self.beat], when=tomorrow)

        response = self.bulk([self.beat, other], when=tomorrow)

        statuses = {r['id']: r['status'] for r in response.data['results']}
        self.assertEqual(statuses[str(self.beat.pk)], 'skipped')
        self.assertEqual(statuses[str(other.pk)], 'applied')
        # The second beat really was created: an IntegrityError on the first
        # must not poison the transaction the rest of the loop runs in.
        self.assertTrue(BeatPlan.objects.filter(beat=other, date=tomorrow).exists())

    def test_bulk_refuses_a_date_in_the_past_for_every_beat(self):
        other = self.second_beat()

        response = self.bulk(
            [self.beat, other], when=timezone.localdate() - timedelta(days=1)
        )

        self.assertEqual(response.data['applied'], 0)
        self.assertEqual(response.data['failed'], 2)

    def test_bulk_refuses_the_same_beat_listed_twice(self):
        response = self.bulk([self.beat, self.beat])

        # Letting it through would report one success and one "already
        # planned" for something the user asked for once.
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_refuses_an_empty_selection(self):
        response = self.client.post(
            self.url('plans-bulk'),
            {'beats': [], 'date': str(timezone.localdate())},
            format='json',
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_refuses_somebody_elses_beat(self):
        theirs = self.second_beat(code='DEL-S-01', assigned_user=self.colleague)

        response = self.bulk([theirs])

        self.assertEqual(response.data['applied'], 0)
        self.assertIn('someone else', response.data['results'][0]['detail'].lower())

    def test_bulk_needs_the_planning_permission(self):
        self.authenticate(self.outsider)

        response = self.bulk([self.beat])

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # --------------------------------------------------------------- bulk start

    def test_bulk_start_moves_a_planned_run_into_progress(self):
        plan = self.make_plan()

        response = self.client.post(
            self.url('plans-bulk-start'), {'plans': [str(plan.pk)]}, format='json'
        )

        plan.refresh_from_db()
        self.assertEqual(response.data['applied'], 1)
        self.assertEqual(plan.status, BeatPlanStatus.IN_PROGRESS)
        self.assertIsNotNone(plan.started_at)

    def test_bulk_start_twice_is_not_an_error(self):
        plan = self.make_plan()
        url = self.url('plans-bulk-start')
        self.client.post(url, {'plans': [str(plan.pk)]}, format='json')

        response = self.client.post(url, {'plans': [str(plan.pk)]}, format='json')

        # A phone that retried cannot tell the difference, so neither does this.
        self.assertEqual(response.data['applied'], 1)

    def test_bulk_start_skips_a_closed_plan_with_a_reason(self):
        plan = self.make_plan()
        plan.status = BeatPlanStatus.COMPLETED
        plan.save(update_fields=['status'])

        response = self.client.post(
            self.url('plans-bulk-start'), {'plans': [str(plan.pk)]}, format='json'
        )

        row = response.data['results'][0]
        self.assertEqual(row['status'], 'skipped')
        self.assertIn('already closed', row['detail'].lower())

    def test_bulk_start_does_not_touch_another_users_plan(self):
        theirs = BeatPlan.objects.create(
            beat=self.beat, user=self.colleague, date=timezone.localdate()
        )

        response = self.client.post(
            self.url('plans-bulk-start'), {'plans': [str(theirs.pk)]}, format='json'
        )

        # Not 403: saying "forbidden" would confirm the plan exists.
        row = response.data['results'][0]
        self.assertEqual(row['status'], 'rejected')
        self.assertIn('no such plan', row['detail'].lower())

        theirs.refresh_from_db()
        self.assertEqual(theirs.status, BeatPlanStatus.PLANNED)

    def test_bulk_start_needs_the_planning_permission(self):
        plan = self.make_plan()
        self.authenticate(self.outsider)

        response = self.client.post(
            self.url('plans-bulk-start'), {'plans': [str(plan.pk)]}, format='json'
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ------------------------------------------------------- detail panel fields

    def test_a_plan_carries_its_zone_and_who_runs_it(self):
        plan = self.make_plan()

        response = self.client.get(self.url('plan-detail', pk=plan.pk))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['user_code'], self.user.employee_code)
        self.assertEqual(response.data['user_name'], self.user.full_name)
        self.assertEqual(response.data['beat_area'], self.beat.area)
        self.assertEqual(response.data['beat_city'], self.beat.city)
        # Present even with no territory set, so the detail panel renders a
        # blank field rather than crashing on a missing key.
        self.assertIn('territory', response.data)
