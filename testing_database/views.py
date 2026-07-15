from rest_framework.decorators import api_view
from rest_framework.response import Response
from .models import HelloTable
from .serializers import HelloSerializer

@api_view(['GET'])
def get_hello_message(request):
    
    messages = HelloTable.objects.all()

    serializer = HelloSerializer(messages, many=True)

    return Response(serializer.data)