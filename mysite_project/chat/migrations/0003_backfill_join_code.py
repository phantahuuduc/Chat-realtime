"""Sinh join_code cho các conversation đã tồn tại trước khi có trường này."""

import secrets

from django.db import migrations

ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'


def backfill(apps, schema_editor):
    Conversation = apps.get_model('chat', 'Conversation')
    used = set(
        Conversation.objects.exclude(join_code=None).values_list('join_code', flat=True)
    )
    for conv in Conversation.objects.filter(join_code=None):
        while True:
            code = ''.join(secrets.choice(ALPHABET) for _ in range(6))
            if code not in used:
                used.add(code)
                break
        conv.join_code = code
        conv.save(update_fields=['join_code'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('chat', '0002_conversation_description_conversation_join_code_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
