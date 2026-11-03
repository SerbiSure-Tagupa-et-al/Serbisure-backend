"""Isolated local PostgreSQL test database; never run this suite against a remote host."""
from .settings import *
import uuid
from copy import deepcopy

DATABASES = deepcopy(DATABASES)
if DATABASES['default'].get('HOST') not in ('localhost', '127.0.0.1', '::1'):
    raise RuntimeError('Urgent-fix QA requires a local PostgreSQL connection.')
DATABASES['default']['TEST'] = {'NAME': 'test_serbisure_qa_' + uuid.uuid4().hex[:12]}


# Speed up fixtures only. Production settings keep Django's normal password hashers.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
# Presence tests must never use a developer's remote Redis instance.
REDIS_URL = None
