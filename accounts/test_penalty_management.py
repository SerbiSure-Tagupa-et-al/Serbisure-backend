import re
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from rest_framework.test import APITestCase

from booking.models import tbl_booking
from .penalties import current_penalty_month
from .penalty_models import PenaltyAdjustment
from .penalty_report import penalty_pdf

User = get_user_model()
BASE = '/api/v1/accounts/admin/penalties/'


class PenaltyManagementTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.publish = patch('accounts.penalty_management.publish').start()
        self.addCleanup(patch.stopall)
        self.admin = self.person('Admin')
        self.desk = self.person('Barangay')
        self.home = self.person('Homeowner')
        self.worker = self.person('Kasambahay', barangay='Carmen')

    def person(self, role, **values):
        n = User.objects.count() + 1
        fields = dict(barangay='Pagatpat', city='City of Cagayan de Oro', province='Misamis Oriental', penalty_points=1)
        fields.update(values)
        return User.objects.create_user(username=f'penalty{n}', email=f'penalty{n}@example.test',
            password='TestPassword123!', first_name=f'Person {n}', account_type=role,
            contact_number=f'+639{n:09}', **fields)

    def body(self, user=None, delta=1, **values):
        user = user or self.home
        return dict(request_id=str(uuid.uuid4()), points_delta=delta, expected_points=user.penalty_points,
            expected_month=current_penalty_month().isoformat(), reason='Verified cancellation review.', **values)

    def send(self, user=None, actor=None, data=None):
        user = user or self.home
        self.client.force_authenticate(actor or self.admin)
        return self.client.post(f'{BASE}users/{user.pk}/', data or self.body(user), format='json')

    def test_barangay_scope_ignores_client_override_and_superadmin_sees_all_residents(self):
        self.client.force_authenticate(self.desk)
        response = self.client.get(BASE + 'users/?barangay=Carmen')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['id'] for row in response.data['results']], [str(self.home.pk)])
        self.assertEqual(self.client.get(f'{BASE}users/{self.worker.pk}/').status_code, 404)
        self.assertEqual(self.send(self.worker, self.desk).status_code, 404)
        self.client.force_authenticate(self.admin)
        data = self.client.get(BASE + 'users/').data
        self.assertEqual(data['count'], 2)
        self.assertEqual(self.client.get(BASE + 'users/?barangay=Carmen').data['count'], 1)

    def test_normalized_jurisdiction_matches_but_conflicting_city_missing_location_and_staff_do_not(self):
        self.home.barangay = ' Brgy. PAGATPAT '
        self.home.city = 'Cagayan de Oro City'
        self.home.save()
        self.person('Homeowner', city='Different City')
        self.person('Homeowner', province='')
        self.person('Kasambahay', is_staff=True)
        self.client.force_authenticate(self.desk)
        self.assertEqual(self.client.get(BASE + 'users/').data['count'], 1)
        self.assertEqual(self.send(actor=self.desk).status_code, 201)
        self.desk.barangay = ''
        self.desk.save()
        self.assertEqual(self.client.get(BASE + 'users/').status_code, 403)

    def test_score_filters_use_current_month_scores_and_preserve_jurisdiction(self):
        zero = self.person('Homeowner', penalty_points=0)
        two = self.person('Kasambahay', penalty_points=2)
        three = self.person('Homeowner', penalty_points=3)
        prior = self.person('Homeowner', penalty_points=3,
            penalty_month=(current_penalty_month() - timedelta(days=1)).replace(day=1))
        self.client.force_authenticate(self.desk)
        expected = {'0': [zero, prior], '1': [self.home], '2': [two], '3': [three],
                    'with_penalties': [self.home, two, three]}
        for points, residents in expected.items():
            response = self.client.get(BASE + 'users/', {'points': points, 'barangay': 'Carmen'})
            self.assertEqual(response.status_code, 200, response.data)
            self.assertCountEqual([item['id'] for item in response.data['results']], [str(user.pk) for user in residents])
            if points != 'with_penalties':
                self.assertEqual({item['penalty_points'] for item in response.data['results']}, {int(points)})
        prior.refresh_from_db()
        self.assertEqual(prior.penalty_points, 3)  # A filtered read never mutates saved points.
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(BASE + 'users/?points=1').data['count'], 2)
        self.assertEqual(self.client.get(BASE + 'users/?points=2&role=Homeowner').data['count'], 0)
        self.assertEqual(self.client.get(BASE + 'users/', {'points': '2', 'search': two.first_name}).data['count'], 1)
        for value in ['-1', '4', '1.5', 'invalid']:
            response = self.client.get(BASE + 'users/', {'points': value})
            self.assertEqual(response.status_code, 400)
            self.assertIn('detail', response.data)

    def test_score_filter_is_applied_before_pagination(self):
        for _ in range(11):
            self.person('Homeowner', penalty_points=2)
        self.client.force_authenticate(self.admin)
        first = self.client.get(BASE + 'users/?points=2')
        second = self.client.get(BASE + 'users/?points=2&page=2')
        self.assertEqual(first.data['count'], 11)
        third = self.client.get(BASE + 'users/?points=2&page=3')
        self.assertEqual(len(first.data['results']), 5)
        self.assertEqual(len(second.data['results']), 5)
        self.assertEqual(len(third.data['results']), 1)
        self.assertEqual({item['penalty_points'] for item in first.data['results'] + second.data['results'] + third.data['results']}, {2})

    def test_residents_and_suspended_or_inactive_officials_cannot_read_write_or_export(self):
        for actor in [self.home, self.person('Barangay', barangay='Bugo', is_active=False), self.person('Admin', is_account_suspended=True)]:
            self.client.force_authenticate(actor)
            for path in ['users/', 'history/', 'export/']:
                self.assertEqual(self.client.get(BASE + path).status_code, 403)
            self.assertEqual(self.send(actor=actor).status_code, 403)
        for target in [self.desk, self.admin, self.person('Homeowner', is_staff=True)]:
            self.assertEqual(self.send(target).status_code, 404)

    def test_adjustments_update_restriction_and_leave_account_chat_and_booking_records_alone(self):
        self.home.chat_suspended_until = timezone.now() + timedelta(hours=3)
        self.home.is_account_suspended = True
        self.home.save()
        response = self.send(data=self.body(delta=2))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['user']['penalty_points'], 3)
        self.assertTrue(response.data['user']['is_restricted'])
        self.home.refresh_from_db()
        response = self.send(data=self.body(delta=-1))
        self.assertEqual(response.data['user']['penalty_points'], 2)
        self.assertFalse(response.data['user']['is_restricted'])
        self.home.refresh_from_db()
        self.assertTrue(self.home.is_account_suspended)
        self.assertIsNotNone(self.home.chat_suspended_until)
        self.assertEqual(PenaltyAdjustment.objects.count(), 2)

    def test_duplicate_request_is_idempotent_and_changed_reuse_or_stale_score_conflicts(self):
        body = self.body()
        first = self.send(data=body)
        second = self.send(data=body)
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.data['record']['id'], second.data['record']['id'])
        self.assertEqual(PenaltyAdjustment.objects.count(), 1)
        self.assertEqual(self.send(data={**body, 'reason': 'Different adjustment notes.'}).status_code, 409)
        self.assertEqual(self.send(data=self.body()).status_code, 409)
        self.home.refresh_from_db()
        self.assertEqual(self.home.penalty_points, 2)

    def test_validation_rejects_zero_out_of_range_overdraft_blank_notes_and_unreferenced_verification(self):
        for values in [dict(points_delta=0), dict(points_delta=4), dict(points_delta=-2), dict(reason=' '),
                       dict(reason='short'), dict(report_verified=True), dict(expected_points='invalid')]:
            body = {**self.body(), **values}
            response = self.send(data=body)
            self.assertEqual(response.status_code, 400, response.data)
            self.assertIn('detail', response.data)
        self.assertFalse(PenaltyAdjustment.objects.exists())

    def test_month_reset_uses_effective_score_and_old_confirmation_cannot_adjust_new_month(self):
        old = current_penalty_month() - timedelta(days=1)
        self.home.penalty_month = old.replace(day=1)
        self.home.penalty_points = 3
        self.home.is_restricted = True
        self.home.save()
        self.client.force_authenticate(self.admin)
        response = self.client.get(f'{BASE}users/{self.home.pk}/')
        self.assertEqual(response.data['user']['penalty_points'], 0)
        self.assertFalse(response.data['user']['is_restricted'])
        body = {**self.body(), 'expected_points': 0, 'expected_month': old.replace(day=1).isoformat()}
        self.assertEqual(self.send(data=body).status_code, 409)
        body['expected_month'] = current_penalty_month().isoformat()
        response = self.send(data=body)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['user']['penalty_points'], 1)

    def cancellation(self, user=None, **values):
        user = user or self.home
        data = dict(poster_id=user, booking_type='short_term', booking_status='Cancelled',
            service_category=['Cleaning'], region='Region X', rate_period='day', rate_amount='800',
            start_time=timezone.now(), cancelled_by=user, cancelled_at=timezone.now(),
            cancellation_reason='Safety / Inappropriate Homeowner Behavior', cancellation_penalty_points=1)
        data.update(values)
        return tbl_booking.objects.create(**data)

    def test_review_and_verified_incident_link_only_current_users_cancellation_and_keep_original_penalty(self):
        booking = self.cancellation()
        foreign = self.cancellation(self.worker)
        prior = self.cancellation(cancelled_at=timezone.now().replace(day=1, hour=0, minute=0, second=0) - timedelta(days=2))
        for bad in [foreign, prior]:
            self.assertEqual(self.send(data=self.body(delta=-1, booking_id=str(bad.pk))).status_code, 400)
        body = self.body(delta=-1, booking_id=str(booking.pk), report_verified=True, incident_reference='BLT-2026-001')
        response = self.send(actor=self.desk, data=body)
        self.assertEqual(response.status_code, 201, response.data)
        record = PenaltyAdjustment.objects.get()
        self.assertEqual((record.points_before, record.points_after), (1, 0))
        self.assertEqual(record.admin_id_snapshot, self.desk.pk)
        self.assertTrue(record.report_verified)
        booking.refresh_from_db()
        self.assertEqual(booking.cancellation_penalty_points, 1)
        review = self.client.get(f'{BASE}users/{self.home.pk}/').data
        self.assertEqual(review['cancellations']['count'], 1)
        self.assertEqual(len(review['recent_adjustments']), 1)

    def test_audit_failure_rolls_back_score_and_emits_no_event(self):
        with patch('accounts.penalty_management.PenaltyAdjustment.objects.create', side_effect=RuntimeError('Audit unavailable')):
            with self.assertRaises(RuntimeError):
                self.send()
        self.home.refresh_from_db()
        self.assertEqual(self.home.penalty_points, 1)
        self.publish.assert_not_called()

    def test_history_is_immutable_in_orm_sql_and_api(self):
        self.send()
        record = PenaltyAdjustment.objects.get()
        for action in [record.save, record.delete, lambda: PenaltyAdjustment.objects.update(reason='changed'),
                       lambda: PenaltyAdjustment.objects.all().delete()]:
            with self.assertRaises(ValidationError):
                action()
        for sql in ["UPDATE tbl_penalty_adjustment SET reason='changed'", 'DELETE FROM tbl_penalty_adjustment',
                    'TRUNCATE tbl_penalty_adjustment']:
            if connection.vendor != 'postgresql' and sql.startswith('TRUNCATE'):
                continue
            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql)
        for method in ['put', 'patch', 'delete']:
            self.assertEqual(getattr(self.client, method)(f'{BASE}users/{self.home.pk}/', {}, format='json').status_code, 405)
        self.assertEqual(self.client.post(BASE + 'history/', {}, format='json').status_code, 405)
        self.assertEqual(PenaltyAdjustment.objects.get().reason, record.reason)

    def test_pdf_and_history_are_scoped_and_preserve_plain_text_notes(self):
        body = {**self.body(delta=-1), 'reason': 'Reviewed <report> & notes for Peña.'}
        self.assertEqual(self.send(data=body).status_code, 201)
        self.assertEqual(self.send(self.worker).status_code, 201)
        self.client.force_authenticate(self.desk)
        data = self.client.get(BASE + 'history/?barangay=Carmen').data
        self.assertEqual(data['count'], 1)
        exported = []
        def render(records, **kwargs):
            exported.extend(records)
            return penalty_pdf(exported, **kwargs)
        with patch('accounts.penalty_api.penalty_pdf', side_effect=render):
            response = self.client.get(BASE + 'export/?barangay=Carmen')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('penalty-adjustments.pdf', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF-'))
        self.assertEqual(len(exported), 1)
        self.assertEqual(exported[0].user_id_snapshot, self.home.pk)
        self.assertEqual(exported[0].reason, body['reason'])
        self.assertEqual(exported[0].points_delta, -1)

    def test_pdf_includes_all_filtered_records_and_handles_long_notes_and_empty_results(self):
        reason = ('Verified incident notes with Peña & <plain text>.\n' * 45)[:2000]
        for _ in range(12):
            self.home.refresh_from_db()
            self.assertEqual(self.send(data={**self.body(delta=-1 if self.home.penalty_points else 1),
                'reason': reason}).status_code, 201)
        exported = []
        def render(records, **kwargs):
            exported.extend(records)
            return penalty_pdf(exported, **kwargs)
        with patch('accounts.penalty_api.penalty_pdf', side_effect=render):
            response = self.client.get(BASE + 'export/?role=Homeowner&page=2')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(exported), 12)  # Export all matches, not the current list page.
        self.assertGreater(len(re.findall(rb'/Type\s*/Page\b', response.content)), 1)
        empty = self.client.get(BASE + 'export/?search=NoSuchResident')
        self.assertEqual(empty.status_code, 200)
        self.assertTrue(empty.content.startswith(b'%PDF-'))

    def test_history_identity_and_scope_survive_profile_edits_and_account_deletion(self):
        self.send(actor=self.desk)
        self.home.barangay = 'Carmen'
        self.home.save()
        self.home.delete()
        self.client.force_authenticate(self.desk)
        records = self.client.get(BASE + 'history/').data
        self.assertEqual(records['count'], 1)
        self.assertEqual(records['results'][0]['user_barangay'], 'Pagatpat')
