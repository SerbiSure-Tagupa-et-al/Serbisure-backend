from datetime import timedelta
from uuid import uuid4

from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import tbl_booking
from .services import PH_TIME
from .test_urgent_guards import participants


class ShortTermRangeTests(APITestCase):
    def setUp(self):
        self.owner, self.worker = participants()
        now = timezone.now().astimezone(PH_TIME)
        self.start = (now + timedelta(days=(5-now.weekday()) % 7 or 7)).replace(hour=8, minute=0, second=0, microsecond=0)
        self.payload = dict(recipient_id=str(self.worker.pk), booking_type='short_term', work_setup='stay_out',
            service_category=['Cleaning'], rate_amount='800', rate_period='day', region='Region X', street='Lot 5555',
            start_time=self.start.isoformat(), end_time=(self.start+timedelta(days=13, hours=9)).isoformat(),
            daily_start_time='08:00:00', daily_end_time='17:00:00', work_days=['M', 'T', 'W', 'Th', 'F'])
        self.client.force_authenticate(self.owner)

    def send(self, **changes):
        return self.client.post(reverse('booking-chat-offer'), {**self.payload, **changes}, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))

    def test_range_with_weekdays_is_stored_and_returned_after_acceptance(self):
        response = self.send()
        self.assertEqual(response.status_code, 201, response.data)
        booking = tbl_booking.objects.get(pk=response.data['booking']['booking_id'])
        self.assertEqual(booking.end_time.astimezone(PH_TIME).date(), (self.start+timedelta(days=13)).date())
        self.assertEqual(booking.work_days, self.payload['work_days'])
        self.client.force_authenticate(self.worker)
        accepted = self.client.patch(reverse('booking-accept', kwargs={'booking_id': booking.pk}), {}, format='json')
        self.assertEqual(accepted.status_code, 200, accepted.data)
        for viewer in (self.owner, self.worker):
            self.client.force_authenticate(viewer)
            details = self.client.get(reverse('booking-detail', kwargs={'booking_id': booking.pk})).data
            self.assertEqual(details['end_time'], response.data['booking']['end_time'])
            self.assertNotEqual(details['start_time'][:10], details['end_time'][:10])
            self.assertEqual(details['work_days'], self.payload['work_days'])

    def test_range_requires_at_least_one_selected_workday_inside_it(self):
        response = self.send(end_time=(self.start+timedelta(days=1, hours=9)).isoformat())  # Sat-Sun, Mon-Fri selected.
        self.assertEqual(response.status_code, 400)
        self.assertIn('within the date range', str(response.data))
        self.assertFalse(tbl_booking.objects.exists())

    def test_end_date_before_start_is_rejected(self):
        response = self.send(end_time=(self.start-timedelta(days=1)+timedelta(hours=9)).isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(tbl_booking.objects.exists())
