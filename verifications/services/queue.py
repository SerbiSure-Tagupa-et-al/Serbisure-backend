"""Portal applicants and dashboard counters use the same resident credentials."""
from accounts.admin_statistics import barangay_name, scoped_residents
from accounts.models import tbl_user_profile
from accounts.workforce import credential_snapshot, latest_required_documents
from core.utils import get_signed_cloudinary_url
from verifications.serializers import AdminVerificationQueueSerializer

PORTAL_STATUS = {'verified': 'VERIFIED', 'pending': 'PENDING / REVIEW', 'rejected': 'REJECTED',
                 'expired': 'EXPIRED', 'unverified': 'NO_DOCUMENTS'}


def verification_queue(request):
    users = scoped_residents(request)
    active_lgus = {barangay_name(value).casefold() for value in tbl_user_profile.objects.filter(
        account_type='Barangay', is_active=True,
    ).values_list('barangay', flat=True) if barangay_name(value)}
    statuses = {user.pk: PORTAL_STATUS[credential_snapshot(user)[0]] for user in users}
    documents = [doc for user in users for doc in latest_required_documents(user)]
    documents.sort(key=lambda doc: (doc.created_at, str(doc.pk)), reverse=True)
    rows = list(AdminVerificationQueueSerializer(
        documents, many=True, context={'request': request, 'applicant_statuses': statuses,
                                       'active_lgus': active_lgus, 'resident_scope': True},
    ).data)
    for user in users:
        if latest_required_documents(user):
            continue
        rows.append({
            'id': f'unsubmitted_{user.pk}', 'userId': str(user.pk),
            'name': f'{user.first_name} {user.last_name}'.strip() or user.username,
            'role': user.account_type.upper(), 'barangay': barangay_name(user.barangay) or 'Unassigned',
            'email': user.email, 'contactNumber': user.contact_number,
            'avatar': get_signed_cloudinary_url(user.profile_link, as_avatar=True) or '',
            'documentType': 'No Documents Submitted', 'rawDocumentType': 'unsubmitted',
            'documentNumber': 'Not Available', 'submittedDate': 'Not Submitted',
            'issuedDate': 'N/A', 'validityDate': 'N/A', 'documentImage': '',
            'status': 'NO_DOCUMENTS', 'applicantStatus': 'NO_DOCUMENTS',
            'recordStatus': 'Under Review', 'hasDocuments': False,
            'hasLguCoverage': barangay_name(user.barangay).casefold() in active_lgus,
        })
    status = request.query_params.get('status', 'ALL').upper()
    if status not in ('ALL', ''):
        wanted = {'PENDING': 'PENDING / REVIEW', 'UNSUBMITTED': 'NO_DOCUMENTS'}.get(status, status)
        rows = [row for row in rows if (row['applicantStatus'] if status == 'EXPIRED' else row['status']) == wanted]
    return rows
