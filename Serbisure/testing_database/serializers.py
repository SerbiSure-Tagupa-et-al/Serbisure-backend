from rest_framework import serializers
from .models import HelloTable

class HelloSerializer(serializers.ModelSerializer):
    class Meta:
        model = HelloTable
        fields = ['id', 'message']
        