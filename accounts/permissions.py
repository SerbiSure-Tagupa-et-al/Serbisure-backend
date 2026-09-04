from rest_framework.permissions import BasePermission

class IsKasambahay(BasePermission):
    """
    Allows access only to authenticated users with account_type == 'Kasambahay'.
    Returns 403 Forbidden for other account types (e.g. Homeowner, Admin, Barangay).
    """
    message = "Only Kasambahay accounts can access this resource."

    def has_permission(self, request, view):
        return bool(
            request.user and
            request.user.is_authenticated and
            getattr(request.user, 'account_type', None) == 'Kasambahay'
        )
