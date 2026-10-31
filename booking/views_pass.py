from secrets import token_urlsafe
from uuid import UUID

from django.core import signing
from django.db import DatabaseError
from django.db.models import Prefetch
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe
from rest_framework.throttling import SimpleRateThrottle

from .gate_pass import GATE_PASS_MAX_AGE, GATE_PASS_SALT, pass_details, pass_status
from .models import tbl_booking, tbl_booking_assignment


class PassLookupThrottle(SimpleRateThrottle):
    rate = '60/min'
    scope = 'public_gate_pass'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


@never_cache
@require_safe
def verify_booking_pass(request, invalid_reference=None):
    """Validate the 24-hour signature before reading any booking or personal data."""
    code = 404
    context = pass_status('not_found')
    throttle = PassLookupThrottle()
    if not throttle.allow_request(request, None):
        code = 429
        context.update(pass_status('rate_limited'))
    elif invalid_reference is None and len(request.GET.getlist('token')) == 1:
        try:
            token = request.GET.get('token', '')
            if not token or len(token) > 512:
                raise signing.BadSignature('Invalid pass token')
            value = signing.loads(token, salt=GATE_PASS_SALT, max_age=GATE_PASS_MAX_AGE)
            if not isinstance(value, str):
                raise signing.BadSignature('Invalid pass reference')
            booking_id = UUID(value)
            booking = tbl_booking.objects.select_related('poster_id').prefetch_related(
                'poster_id__documents', Prefetch('assignments', queryset=tbl_booking_assignment.objects.select_related(
                    'accepter_id').prefetch_related('accepter_id__documents')),
            ).filter(pk=booking_id).first()
            if booking is not None:
                context = pass_details(booking)
                code = 200
        except signing.SignatureExpired:
            code = 410
            context.update(pass_status('token_expired'))
        except (signing.BadSignature, ValueError, TypeError):
            # Invalid signatures and signed non-UUID payloads disclose no data.
            pass
        except DatabaseError:
            code = 503
            context.update(pass_status('unavailable'))
    # Permit only this page's copy-button script, not arbitrary inline scripts.
    nonce = token_urlsafe(24)
    context['csp_nonce'] = nonce
    response = render(request, 'booking/pass_details.html', context, status=code)
    response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Frame-Options'] = 'DENY'
    response['Content-Security-Policy'] = f"default-src 'none'; script-src 'nonce-{nonce}'; img-src 'self' https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    if code == 429:
        response['Retry-After'] = '60'
    return response
