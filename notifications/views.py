from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from .models import tbl_notification
from .serializers import NotificationSerializer

class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = tbl_notification.objects.filter(
            receiver_id=request.user
        ).select_related('sender_id').order_by('-createdAt')[:50]
        
        unread_count = tbl_notification.objects.filter(
            receiver_id=request.user,
            notification_state='Unread'
        ).count()

        serializer = NotificationSerializer(notifications, many=True)
        return Response({
            'notifications': serializer.data,
            'unread_count': unread_count,
        }, status=status.HTTP_200_OK)


class NotificationMarkReadView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            notification = tbl_notification.objects.get(
                notification_id=pk,
                receiver_id=request.user
            )
            notification.notification_state = 'Read'
            notification.save(update_fields=['notification_state'])
            return Response({'status': 'success', 'notification_id': str(pk)}, status=status.HTTP_200_OK)
        except tbl_notification.DoesNotExist:
            return Response({'error': 'Notification not found.'}, status=status.HTTP_404_NOT_FOUND)


class NotificationMarkAllReadView(APIView):
    permission_classes = [IsAuthenticated]

    def patch(self, request):
        updated_count = tbl_notification.objects.filter(
            receiver_id=request.user,
            notification_state='Unread'
        ).update(notification_state='Read')
        return Response({
            'status': 'success',
            'marked_count': updated_count
        }, status=status.HTTP_200_OK)
