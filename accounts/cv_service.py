"""Fresh CV snapshots and exports; never parse editable biographies as structured terms."""
import csv
from io import BytesIO, StringIO
from pathlib import Path
from urllib.parse import urlsplit
from xml.sax.saxutils import escape
import requests
from PIL import Image as PillowImage
from django.db.models import Avg
from django.utils import timezone
from core.utils import get_signed_cloudinary_url
from .models import tbl_worker_profile
from .worker_serializers import WorkerProfileSerializer
from .cv_presentation import build as build_presentation

LABELS = {'Child_care': 'Childcare', 'stay_in': 'Stay-in', 'stay_out': 'Stay-out', 'either': 'Either',
          'short_term': 'Short-term', 'long_term': 'Long-term', 'both': 'Both',
          'new': 'First-time worker', 'household': 'Experience in own household', 'paid': 'Previous paid work',
          'under_1': 'Less than 1 year', '1_to_3': '1-3 years', 'over_3': 'More than 3 years',
          'M': 'Mon', 'T': 'Tue', 'W': 'Wed', 'Th': 'Thu', 'F': 'Fri', 'S': 'Sat', 'Su': 'Sun'}


def verified_work_history(user, limit=20):
    """Return area-only completed booking history and referrals for either participant role."""
    from collections import Counter
    from django.db.models import Q
    from reviews.models import tbl_review
    from booking.models import tbl_booking, tbl_booking_assignment

    assigned_booking_ids = list(
        tbl_booking_assignment.objects.filter(
            accepter_id=user,
            booking_id__booking_status='Completed',
        ).values_list('booking_id', flat=True)
    )
    completed_qs = (
        tbl_booking.objects.filter(booking_status='Completed')
        .filter(Q(poster_id=user) | Q(pk__in=assigned_booking_ids))
        .select_related('poster_id')
        .distinct()
    )
    bookings = list(completed_qs)
    booking_pks = [b.pk for b in bookings]

    assignments = {
        a.booking_id_id: a
        for a in tbl_booking_assignment.objects.filter(booking_id_id__in=booking_pks).select_related('accepter_id')
    }
    reviews_by_booking = {
        r.booking_id_id: r
        for r in tbl_review.objects.filter(reviewee_id=user, booking_id_id__in=booking_pks).select_related('reviewer_id')
    }
    recommended_count = tbl_review.objects.filter(reviewee_id=user, would_recommend=True).count()

    def sort_key(b):
        assignment = assignments.get(b.pk)
        completed_dt = getattr(assignment, 'completed_at', None) if assignment else None
        return completed_dt or b.end_time or b.start_time

    bookings.sort(key=sort_key, reverse=True)

    employer_counts = Counter()
    items = []
    for b in bookings:
        assignment = assignments.get(b.pk)
        employer = assignment.accepter_id if (b.poster_id_id == user.pk and assignment) else b.poster_id
        if employer and employer.pk != user.pk:
            employer_counts[str(employer.pk)] += 1
        else:
            employer = None

        if len(items) >= limit:
            continue

        completed_dt = getattr(assignment, 'completed_at', None) if assignment else None
        review = reviews_by_booking.get(b.pk)
        area = ', '.join(part.strip() for part in [b.barangay or '', b.city or ''] if part and part.strip())
        employer_name = (
            ' '.join(p for p in [employer.first_name, employer.last_name] if p).strip() or employer.username
            if employer else ('Kasambahay' if user.account_type == 'Homeowner' else 'Homeowner')
        )
        categories = list(b.service_category or [])
        items.append({
            'booking_id': str(b.pk),
            'service_category': categories,
            'service_labels': [LABELS.get(c, c.replace('_', ' ')) for c in categories],
            'booking_type': b.booking_type,
            'work_setup': b.work_setup,
            'area': area,
            'start_date': b.start_time.date().isoformat() if b.start_time else None,
            'end_date': b.end_time.date().isoformat() if b.end_time else None,
            'completed_at': (completed_dt or b.end_time or b.start_time).isoformat() if (completed_dt or b.end_time or b.start_time) else None,
            'employer_name': employer_name,
            'partner_role': employer.account_type if employer else ('Kasambahay' if user.account_type == 'Homeowner' else 'Homeowner'),
            'employer_verified': bool(employer and employer.verification_status == 'Verified'),
            'employer_avatar': get_signed_cloudinary_url(employer.profile_link, as_avatar=True) if employer else None,
            'rating': review.rating if review else None,
            'would_recommend': review.would_recommend if review else None,
            'nlp_sentiment': review.nlp_sentiment if review else None,
            'feedback': review.unstructured_feedback if review else None,
        })

    repeat_employers_count = sum(1 for count in employer_counts.values() if count >= 2)
    return {
        'user_id': str(user.pk),
        'account_type': user.account_type,
        'summary': {
            'completed_jobs_count': len(bookings),
            'recommended_count': recommended_count,
            'repeat_employers_count': repeat_employers_count,
        },
        'jobs': items,
    }


def snapshot(user):
    from reviews.models import tbl_review
    worker = tbl_worker_profile.objects.filter(user=user).first()
    details = dict(WorkerProfileSerializer(worker or tbl_worker_profile(user=user)).data)
    if not details['service_categories']:
        aliases = {'Child Care': 'Child_care', 'Childcare': 'Child_care', 'Cook': 'Cooking'}
        known = {'Cleaning', 'Cooking', 'Laundry', 'Child_care', 'Caregiver', 'All-around'}
        details['service_categories'] = list(dict.fromkeys(aliases.get(tag, tag) for tag in user.user_tags if aliases.get(tag, tag) in known))
    if not details['work_setup']:
        details['work_setup'] = 'stay_in' if 'Stay-In' in user.user_tags else 'stay_out' if 'Stay-Out' in user.user_tags else ''
    family_shared = details.pop('share_family_details')
    contact_shared = details.pop('share_contact_details')
    if not family_shared:
        details.pop('civil_status')
        details.pop('number_of_children')
    reviews = tbl_review.objects.filter(reviewee_id=user)
    count = reviews.count()
    history = verified_work_history(user, limit=5)
    data = {
        'user_id': str(user.pk), 'name': ' '.join(value for value in [user.first_name, user.middle_name, user.last_name] if value).strip() or user.username,
        'location': ', '.join(value for value in [user.city, user.province] if value),
        'languages': user.language or '', 'profile_image_url': get_signed_cloudinary_url(user.profile_link, as_avatar=True),
        'verification_status': user.verification_status, 'contact_number': user.contact_number if contact_shared else None,
        'about': '' if user.user_about == 'No Bio' else user.user_about,
        'details': details, 'self_reported': True,
        'performance': {'completed_jobs': history['summary']['completed_jobs_count'], 'review_count': count,
                        'recommended_count': history['summary']['recommended_count'],
                        'repeat_employers_count': history['summary']['repeat_employers_count'],
                        'average_rating': round(reviews.aggregate(value=Avg('rating'))['value'], 1) if count else None,
                        'positive_percentage': round(reviews.filter(nlp_sentiment='Positive').count() * 100 / count) if count else None},
        'work_history': history['jobs'],
        'generated_at': timezone.now().isoformat(),
        'notice': 'Experience, skills, family information and preferences are self-reported. Preferences are not agreed booking or contract terms.',
    }
    data['presentation'] = build_presentation(data, LABELS)
    return data


def display(value):
    if value is None or value == '':
        return 'Not provided'
    if isinstance(value, list):
        return ', '.join(LABELS.get(str(item), str(item)) for item in value) or 'Not provided'
    return LABELS.get(str(value), str(value))


def cv_rows(data):
    d = data['details']
    rows = [('Services offered', display(d['service_categories'])), ('Languages', display(data['languages'])),
            ('Work setup', display(d['work_setup'])), ('Engagement preference', display(d['engagement_preference']))]
    for field, title, unit in [('desired_monthly_rate', 'Expected monthly salary', 'month'), ('desired_daily_rate', 'Expected daily rate', 'day')]:
        rows.append((title, f'PHP {float(d[field]):,.2f}/{unit}' if d[field] is not None else 'Not provided'))
    rows.extend([('Experience', display(d['experience_level'])), ('Experience duration', display(d['experience_duration'])),
                 ('Practical skills', display(d['practical_skills'])), ('Available from', display(d['available_from'])),
                 ('Available days', display(d['available_days'])), ('Preferred shifts', display(d['preferred_shifts'])),
                 ('Preferred work areas', display(d['preferred_work_areas']))])
    if 'civil_status' in d:
        rows.extend([('Civil status', display(d['civil_status'])), ('Number of children', display(d['number_of_children']))])
    if d['education']:
        rows.append(('Education', d['education']))
    rows.append(('References', 'Available on request' if d['references_available'] else 'Not provided'))
    return rows


def csv_bytes(data):
    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t', '\r', '\n')) else value
    rows = [('Name', data['name']), ('Location', data['location']), ('Verification status', data['verification_status']),
            ('Contact', data['contact_number'] or 'Contact through SerbiSure'), *cv_rows(data),
            ('Experience summary', data['details']['experience_summary']),
            ('Training (self-reported)', '; '.join(f"{item['name']} | {item['provider']} | {item['year'] or ''}" for item in data['details']['training'])),
            ('Completed jobs', data['performance']['completed_jobs']), ('Review count', data['performance']['review_count']),
            ('Average rating', data['performance']['average_rating'] if data['performance']['average_rating'] is not None else 'No reviews yet'),
            ('Positive percentage', data['performance']['positive_percentage'] if data['performance']['positive_percentage'] is not None else 'No reviews yet'),
            ('Generated at', data['generated_at'])]
    output = StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow([key for key, _ in rows])
    writer.writerow([safe(value) for _, value in rows])
    return output.getvalue().encode('utf-8-sig')


def _photo(url):
    if not url or urlsplit(url).scheme != 'https' or urlsplit(url).hostname != 'res.cloudinary.com':
        return None
    try:
        with requests.get(url, stream=True, timeout=(2, 3), allow_redirects=False) as response:
            if response.status_code != 200:
                return None
            raw = BytesIO()
            for chunk in response.iter_content(16384):
                raw.write(chunk)
                if raw.tell() > 2 * 1024 * 1024:
                    return None
        raw.seek(0)
        image = PillowImage.open(raw)
        image.thumbnail((256, 256))
        result = BytesIO()
        image.convert('RGB').save(result, format='JPEG')
        result.seek(0)
        return result
    except (requests.RequestException, OSError, ValueError, PillowImage.DecompressionBombError):
        return None


def pdf_bytes(data):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Flowable, KeepTogether
    from PIL import ImageOps
    assets = Path(__file__).parent / 'assets'
    for name, file in [('CvRegular', 'Nunito-Regular.ttf'), ('CvBold', 'Nunito-Bold.ttf')]:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(assets / file)))
    ink, muted, accent, rule, tint = [colors.HexColor(value) for value in ['#111827', '#6B7280', '#9A3412', '#D1D5DB', '#F8F5F1']]
    body = ParagraphStyle('body', fontName='CvRegular', fontSize=10.5, leading=15, textColor=ink, splitLongWords=True)
    title = ParagraphStyle('title', parent=body, fontName='CvBold', fontSize=22 if len(data['name']) > 28 else 28, leading=27 if len(data['name']) > 28 else 32, spaceAfter=5)
    headline = ParagraphStyle('headline', parent=body, fontSize=13, leading=18, textColor=accent, spaceAfter=6)
    small = ParagraphStyle('small', parent=body, fontSize=9, leading=12, textColor=muted)
    fact_value = ParagraphStyle('fact', parent=body, fontName='CvBold', fontSize=11, leading=15)
    def p(text, style=body):
        return Paragraph(escape(str(text)).replace('\n', '<br/>'), style)
    class CirclePhoto(Flowable):
        def __init__(self, photo):
            super().__init__()
            self.width = self.height = 80
            # Crop a square without stretching a portrait or landscape photo.
            image = ImageOps.exif_transpose(PillowImage.open(photo))
            self.image = ImageReader(ImageOps.fit(image, (256, 256), centering=(.5, .3)))
        def draw(self):
            canvas = self.canv
            canvas.saveState()
            path = canvas.beginPath()
            path.circle(40, 40, 40)
            canvas.clipPath(path, stroke=0, fill=0)
            canvas.drawImage(self.image, 0, 0, 80, 80, mask='auto')
            canvas.restoreState()
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=A4, rightMargin=56, leftMargin=56, topMargin=48, bottomMargin=48, title=f"{data['name']} - SerbiSure CV", author='SerbiSure')
    model = build_presentation(data, LABELS)
    info = [p('SERBISURE | KASAMBAHAY CV', small), Spacer(1, 8), p(model['name'], title)]
    if model['services']:
        info.append(p(model['services'], headline))
    meta = ' · '.join(value for value in [model['location'], model['phone']] if value)
    if meta:
        info.append(p(meta))
    if model['verified']:
        info.extend([Spacer(1, 5), p('Verified', headline)])
    photo = _photo(data.get('profile_image_url'))
    if photo:
        header = Table([[info, CirclePhoto(photo)]], colWidths=[doc.width - 96, 96], hAlign='LEFT')
        header.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 0), ('ALIGN', (1, 0), (1, 0), 'RIGHT')]))
        story = [header]
    else:
        story = info
    if model['facts']:
        facts = [[p(item['label'].upper(), small), Spacer(1, 4), p(item['value'], fact_value)] for item in model['facts']]
        table = Table([facts], colWidths=[doc.width / len(facts)] * len(facts), hAlign='LEFT')
        table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), tint), ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 10), ('RIGHTPADDING', (0, 0), (-1, -1), 10), ('TOPPADDING', (0, 0), (-1, -1), 12), ('BOTTOMPADDING', (0, 0), (-1, -1), 12)]))
        story.extend([Spacer(1, 20), table])
    section_style = ParagraphStyle('section', parent=body, fontName='CvBold', fontSize=11, leading=14, textColor=accent, spaceBefore=20, spaceAfter=8, keepWithNext=True)
    for section in model['sections']:
        story.append(p(section['title'].upper(), section_style))
        if section['kind'] == 'pairs':
            items = section['items']
            cells = [[p(item['label'].upper(), small), Spacer(1, 3), p(item['value'])] for item in items]
            rows = [cells[i:i + 2] + ([''] if len(cells[i:i + 2]) == 1 else []) for i in range(0, len(cells), 2)]
            table = Table(rows, colWidths=[doc.width / 2] * 2, hAlign='LEFT')
            table.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'TOP'), ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 16), ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 9)]))
            story.append(table)
        elif section['kind'] == 'chips':
            chips = section['items'][:12]
            if len(section['items']) > 12:
                chips.append(f"+{len(section['items']) - 12} more")
            row, widths, total = [], [], 0
            def add_chip_row():
                table = Table([row], colWidths=widths, hAlign='LEFT')
                table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), tint), ('BOX', (0, 0), (-1, -1), .5, rule), ('LEFTPADDING', (0, 0), (-1, -1), 9), ('RIGHTPADDING', (0, 0), (-1, -1), 9), ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4)]))
                story.extend([table, Spacer(1, 5)])
            for chip in chips:
                width = min(doc.width, pdfmetrics.stringWidth(chip, 'CvRegular', 10.5) + 20)
                if row and total + width > doc.width:
                    add_chip_row()
                    row, widths, total = [], [], 0
                row.append(p(chip)); widths.append(width); total += width
            if row:
                add_chip_row()
        else:
            for value in section['items']:
                story.extend([p(value), Spacer(1, 4)])
    trust = [Spacer(1, 16)] + [p(value, small) for value in model['trust']]
    trust.extend([Spacer(1, 8), p(model['notice'], small), Spacer(1, 6), p(f"Generated {model['generated_date']} | Based on current saved profile", small)])
    story.append(KeepTogether(trust))
    def footer(canvas, document):
        canvas.setFont('CvRegular', 9)
        canvas.setFillColor(muted)
        canvas.drawRightString(A4[0] - 56, 28, f'Page {document.page}')
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
