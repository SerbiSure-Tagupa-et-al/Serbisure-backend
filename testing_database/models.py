from django.db import models

class HelloTable(models.Model):
    message = models.CharField(max_length=255)

    def __str__(self):
        return self.message