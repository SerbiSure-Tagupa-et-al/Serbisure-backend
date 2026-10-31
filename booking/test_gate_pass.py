from datetime import datetime, time, timedelta
from unittest.mock import patch
from urllib.parse import urlencode
from uuid import uuid4

from django.core.cache import cache
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import tbl_user_profile as User
from accounts.workforce import PH_TIME
from booking.models import tbl_booking as Booking, tbl_booking_assignment as Assignment
from booking.gate_pass import generate_gate_pass_token


class GatePassTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = datetime(2026, 10, 31, 10, 35, tzinfo=PH_TIME)
        self.owner = User.objects.create_user(
            username='pass-owner', email='private-owner@example.test', account_type='Homeowner',
            first_name='Maria', middle_name='Cruz', last_name='Santos', contact_number='+639123456789',
            profile_link='https://example.test/resident.jpg', verification_status='Verified',
        )
        self.worker = User.objects.create_user(
            username='pass-worker', email='private-worker@example.test', account_type='Kasambahay',
            first_name='Ana', middle_name='Reyes', last_name='Ramos', contact_number='+639987654321',
            profile_link='https://example.test/worker.jpg', verification_status='Verified',
        )
        self.worker.documents.update(document_number='PRIVATE-GOVERNMENT-ID-1234')
        self.booking = Booking.objects.create(
            poster_id=self.owner, booking_status='Accepted', booking_type='short_term',
            service_category=['Cleaning'], start_time=self.now.replace(hour=8, minute=0),
            end_time=self.now.replace(hour=17, minute=0), work_days=['S'],
            daily_start_time=time(8), daily_end_time=time(17), rate_amount='12345.67', rate_period='day',
            street='Block 12, Lot 5, Phase 2, Morning Mist Village', floor_number='Unit 3',
            barangay='Iponan', city='Cagayan de Oro', province='Misamis Oriental', zip_code='9000',
        )
        self.assignment = Assignment.objects.create(booking_id=self.booking, accepter_id=self.worker)
        self.url = reverse('booking-public-pass') + '?' + urlencode({'token': generate_gate_pass_token(self.booking.pk)})

    def check(self, now=None):
        with patch('booking.gate_pass.timezone.now', return_value=now or self.now):
            return self.client.get(self.url)

    def test_anonymous_live_pass_has_correct_people_address_and_no_financial_or_id_data(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.check()
        self.assertContains(response, 'OK to enter')
        for expected in ('Ana Reyes Ramos', 'Maria Cruz Santos', self.booking.full_address,
                         'worker.jpg', 'resident.jpg', 'tel:+639123456789', 'Verified ID',
                         '8:00 AM – 5:00 PM', 'Cleaning', f'BK-{str(self.booking.pk)[:8].upper()}'):
            self.assertContains(response, expected)
        for secret in ('12345.67', 'PRIVATE-GOVERNMENT-ID-1234', 'private-owner@example.test',
                       'private-worker@example.test', 'rate_amount', 'document_number'):
            self.assertNotContains(response, secret)
        self.assertFalse(any(query['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for query in queries))
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertIn('noindex', response['X-Robots-Tag'])
        self.assertIn("frame-ancestors 'none'", response['Content-Security-Policy'])

    def test_worker_posted_booking_correctly_resolves_the_homeowner_as_accepter(self):
        self.booking.poster_id = self.worker; self.booking.save()
        self.assignment.accepter_id = self.owner; self.assignment.save()
        response = self.check()
        self.assertEqual(response.context['worker']['name'], 'Ana Reyes Ramos')
        self.assertEqual(response.context['homeowner']['name'], 'Maria Cruz Santos')
        self.assertContains(response, 'tel:+639123456789')

    def test_pending_cancelled_completed_and_expired_do_not_expose_private_people_details(self):
        for status in ('Pending', 'Cancelled', 'Completed'):
            self.booking.booking_status = status; self.booking.save()
            response = self.check()
            self.assertFalse(response.context['entry_allowed'])
            self.assertEqual(response.context['pass_state'], {'Pending': 'pending', 'Cancelled': 'cancelled', 'Completed': 'ended'}[status])
            self.assertContains(response, 'Do not enter')
            for private in ('Maria Cruz Santos', 'Ana Reyes Ramos', 'Morning Mist Village', '+639123456789', 'worker.jpg'):
                self.assertNotContains(response, private)
        self.booking.booking_status = 'Accepted'; self.booking.save()
        response = self.check(self.now.replace(hour=17, minute=0))
        self.assertEqual(response.context['pass_state'], 'ended')
        self.assertNotContains(response, 'Morning Mist Village')

    def test_future_off_day_and_outside_hours_never_authorize_entry(self):
        for at in (self.now - timedelta(days=1), self.now.replace(hour=7), self.now.replace(hour=17)):
            self.assertFalse(self.check(at).context['entry_allowed'])
        self.booking.end_time += timedelta(days=30)
        self.booking.booking_type = 'long_term'; self.booking.rate_period = 'month'; self.booking.save()
        self.assertFalse(self.check(self.now + timedelta(days=1)).context['entry_allowed'])
        self.assertTrue(self.check().context['entry_allowed'])
        self.booking.booking_status = 'InProgress'; self.booking.save()
        self.assertTrue(self.check().context['entry_allowed'])

    def test_open_ended_uses_daily_hours_and_philippine_day(self):
        self.booking.booking_type = 'long_term'; self.booking.rate_period = 'month'
        self.booking.end_time = None; self.booking.work_days = ['S']; self.booking.save()
        response = self.check(self.now + timedelta(days=7))
        self.assertTrue(response.context['entry_allowed'])
        self.assertContains(response, 'onwards (no end date)')
        # 16:00 UTC is the next calendar day in the Philippines.
        self.assertFalse(self.check(datetime.fromisoformat('2026-10-31T16:00:00+00:00')).context['entry_allowed'])

    def test_missing_assignment_invalid_roles_inactive_and_expired_documents_fail_closed(self):
        self.assignment.delete()
        self.assertEqual(self.check().context['pass_state'], 'identity_check')
        self.assignment = Assignment.objects.create(booking_id=self.booking, accepter_id=self.owner)
        self.assertEqual(self.check().context['pass_state'], 'identity_check')
        self.assignment.accepter_id = self.worker; self.assignment.save()
        self.worker.is_active = False; self.worker.save()
        self.assertEqual(self.check().context['pass_state'], 'identity_check')
        self.worker.is_active = True; self.worker.save()
        self.worker.documents.filter(document_type='police_clearance').update(valid_until=self.now.date() - timedelta(days=1))
        response = self.check()
        self.assertFalse(response.context['entry_allowed'])
        self.assertContains(response, 'Call the homeowner to check the IDs.')
        self.assertNotContains(response, 'Verified ID')

    def test_copy_script_has_a_fresh_nonce_and_arbitrary_inline_scripts_stay_blocked(self):
        first, second = self.check(), self.check()
        nonce = first.context['csp_nonce']
        self.assertNotEqual(nonce, second.context['csp_nonce'])
        self.assertContains(first, f'<script nonce="{nonce}">')
        self.assertIn(f"script-src 'nonce-{nonce}'", first['Content-Security-Policy'])
        scripts_policy = first['Content-Security-Policy'].split('script-src ', 1)[1].split(';', 1)[0]
        self.assertNotIn('unsafe-inline', scripts_policy)

    def test_legacy_schedule_uses_real_single_day_times_but_never_invents_multi_day_hours(self):
        self.booking.daily_start_time = None; self.booking.daily_end_time = None
        self.booking.work_days = []; self.booking.save()
        self.assertTrue(self.check().context['entry_allowed'])
        self.booking.end_time += timedelta(days=4); self.booking.save()
        response = self.check()
        self.assertFalse(response.context['entry_allowed'])
        self.assertEqual(response.context['pass_state'], 'incomplete')
        self.assertContains(response, 'Details are missing.')

    def test_unknown_uuid_short_code_and_malformed_id_render_friendly_invalid_page(self):
        unknown = reverse('booking-public-pass') + '?' + urlencode({'token': generate_gate_pass_token(uuid4())})
        self.assertEqual(self.client.get(unknown).status_code, 404)
        for reference in (str(uuid4()), 'BK-12345678', 'not-a-uuid'):
            response = self.client.get(f'/verify/{reference}/')
            self.assertContains(response, 'This code is not in SerbiSure.', status_code=404)
            self.assertNotContains(response, 'Morning Mist Village', status_code=404)
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_live_cancellation_takes_effect_on_next_scan(self):
        self.assertTrue(self.check().context['entry_allowed'])
        Booking.objects.filter(pk=self.booking.pk).update(booking_status='Cancelled')
        self.assertFalse(self.check().context['entry_allowed'])

    def test_user_supplied_names_are_html_escaped(self):
        self.worker.first_name = '<script>alert(1)</script>'; self.worker.save()
        response = self.check()
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>')

    def test_database_failure_does_not_claim_that_a_pass_is_valid(self):
        with patch('booking.views_pass.tbl_booking.objects.select_related', side_effect=DatabaseError('offline')):
            response = self.check()
        self.assertContains(response, 'We could not check. Please try again.', status_code=503)
        self.assertIn('no-store', response['Cache-Control'])

    def test_public_lookups_are_rate_limited(self):
        for _ in range(60):
            response = self.client.get('/verify/not-a-uuid/', REMOTE_ADDR='192.0.2.15')
            self.assertEqual(response.status_code, 404)
        response = self.client.get(self.url, REMOTE_ADDR='192.0.2.15')
        self.assertContains(response, 'Please wait a minute, then try again.', status_code=429)
        self.assertEqual(response['Retry-After'], '60')

    def test_real_booking_explains_the_wrong_time_and_shows_the_next_actual_work_window(self):
        early = self.check(self.now.replace(hour=7))
        self.assertEqual(early.context['pass_state'], 'wrong_time')
        self.assertContains(early, 'Real booking. Outside work hours.')
        self.assertContains(early, 'Valid: Today · 8:00 AM – 5:00 PM')
        future = self.check(self.now - timedelta(days=1))
        self.assertContains(future, 'Real booking. It is for another day.')
        self.assertContains(future, 'Valid: Sat, Oct 31 · 8:00 AM – 5:00 PM')
        self.booking.end_time += timedelta(days=30)
        self.booking.booking_type = 'long_term'; self.booking.rate_period = 'month'; self.booking.save()
        rest_day = self.check(self.now + timedelta(days=1))
        self.assertContains(rest_day, 'Real booking. Today is not a work day.')
        self.assertContains(rest_day, 'Valid: Sat, Nov 7 · 8:00 AM – 5:00 PM')
        after_hours = self.check(self.now.replace(hour=18))
        self.assertContains(after_hours, 'Valid: Sat, Nov 7 · 8:00 AM – 5:00 PM')
        self.assertNotContains(after_hours, 'Confirmed')

    def test_next_work_window_never_goes_past_the_saved_end_date(self):
        self.booking.end_time += timedelta(days=1)
        self.booking.save()
        response = self.check(self.now.replace(hour=18))
        self.assertFalse(response.context['entry_allowed'])
        self.assertContains(response, 'No work time left. Call the homeowner.')

    def test_banner_icons_and_words_do_not_depend_on_color_alone(self):
        from booking.gate_pass import PASS_WORDING, pass_status
        core_states = ['allowed', 'wrong_time', 'pending', 'cancelled', 'ended', 'identity_check', 'incomplete', 'not_found']
        self.assertEqual(len({PASS_WORDING[state][1] for state in core_states}), 8)
        for state in core_states:
            data = pass_status(state)
            self.assertEqual(data['entry_allowed'], state == 'allowed')
            self.assertEqual(data['title'], 'OK to enter' if state == 'allowed' else ('Do not enter' if data['status_class'] == 'danger' else 'Do not enter yet'))
