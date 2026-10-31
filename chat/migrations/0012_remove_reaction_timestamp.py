from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('chat', '0011_message_visibility_per_participant')]
    operations = [migrations.RemoveField(model_name='tbl_chat_reaction', name='reacted_at')]
