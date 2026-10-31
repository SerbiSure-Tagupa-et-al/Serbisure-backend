from datetime import timedelta
from uuid import uuid4

from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import tbl_booking
from .services import PH_TIME
from .test_urgent_guards import participants


class AddressFormattingTests(SimpleTestCase):
    def test_complete_address_orders_street_unit_barangay_city_province_and_postal_code(self):
        for barangay in ('Pagatpat', 'Brgy. Pagatpat', 'Barangay Pagatpat'):
            booking = tbl_booking(street=' Lot 5555 ', floor_number=' Unit 2B ', barangay=barangay,
                city='City of Cagayan de Oro', province='Misamis Oriental', zip_code='9000')
            self.assertEqual(booking.full_address,
                'Lot 5555, Unit 2B, Brgy. Pagatpat, City of Cagayan de Oro, Misamis Oriental, 9000')

    def test_optional_and_missing_fields_never_create_blank_separators_or_invent_an_address(self):
        booking = tbl_booking(street='Lot 5555', floor_number=None, barangay=' ', city=None, province='', zip_code=None)
        self.assertEqual(booking.full_address, 'Lot 5555')
        booking.street = ''
        self.assertEqual(booking.full_address, '')


class BookingAddressRoundTripTests(APITestCase):
    def test_address_is_saved_and_visible_to_both_parties_before_and_after_confirmation(self):
        owner, worker = participants()
        address = dict(region='Region X', province='Misamis Oriental', city='City of Cagayan de Oro',
            barangay='Pagatpat', street='Lot 5555', floor_number='Unit 2B', zip_code='9000')
        expected = 'Lot 5555, Unit 2B, Brgy. Pagatpat, City of Cagayan de Oro, Misamis Oriental, 9000'
        for booking_type in ('short_term', 'long_term'):
            for sender, recipient in ((owner, worker), (worker, owner)):
                start = (timezone.now().astimezone(PH_TIME)+timedelta(days=2)).replace(hour=8, minute=0, second=0, microsecond=0)
                payload = {**address, 'recipient_id': str(recipient.pk), 'booking_type': booking_type,
                    'work_setup': 'stay_out', 'service_category': ['Cleaning'], 'rate_amount': '8000',
                    'rate_period': 'day' if booking_type == 'short_term' else 'month', 'start_time': start.isoformat(),
                    'end_time': (start+timedelta(days=30 if booking_type == 'long_term' else 0, hours=9)).isoformat(),
                    'daily_start_time': '08:00:00', 'daily_end_time': '17:00:00',
                    'work_days': [['M', 'T', 'W', 'Th', 'F', 'S', 'Su'][start.weekday()]]}
                self.client.force_authenticate(sender)
                response = self.client.post(reverse('booking-chat-offer'), payload, format='json', HTTP_IDEMPOTENCY_KEY=str(uuid4()))
                self.assertEqual(response.status_code, 201, response.data)
                booking_id = response.data['booking']['booking_id']
                saved = tbl_booking.objects.get(pk=booking_id)
                for field, value in address.items():
                    self.assertEqual(getattr(saved, field), value)
                self.assertEqual(response.data['booking']['full_address'], expected)
                self.assertEqual(response.data['chat_message']['booking_offer']['booking']['full_address'], expected)
                self.client.force_authenticate(recipient)
                confirmed = self.client.patch(reverse('booking-accept', kwargs={'booking_id': booking_id}), {}, format='json')
                self.assertEqual(confirmed.status_code, 200, confirmed.data)
                for viewer in (sender, recipient):
                    self.client.force_authenticate(viewer)
                    details = self.client.get(reverse('booking-detail', kwargs={'booking_id': booking_id})).data
                    self.assertEqual(details['full_address'], expected)
                    self.assertEqual(details['floor_number'], 'Unit 2B')
                    self.assertEqual(details['zip_code'], '9000')
                    if booking_type == 'long_term':
                        self.assertEqual(details['contract_summary']['location'], expected)
                    thread = self.client.get(reverse('chat-thread', kwargs={'partner_id': recipient.pk if viewer == sender else sender.pk})).data
                    messages = thread['data'] if isinstance(thread, dict) else thread
                    offer = next(row for row in messages if row['booking_offer'] and row['booking_offer']['booking']['booking_id'] == booking_id)
                    self.assertEqual(offer['booking_offer']['booking']['full_address'], expected)
