"""Settings available only inside the dedicated disposable QA runner."""
import os
from pathlib import Path

from tests.campaign_qa.exceptions import QASafetyError

run_id = os.environ.get('CAMPAIGN_QA_RUN')
socket_dir = Path(os.environ.get('CAMPAIGN_QA_SOCKET', '/missing')).resolve()
if not run_id or not socket_dir.is_dir() or (socket_dir.parent / 'QA_ONLY').read_text() != run_id:
    raise QASafetyError('Campaign QA settings require the runner-owned temporary cluster')
if os.environ.get('DATABASE_URL'):
    raise QASafetyError('No external DATABASE_URL is permitted')

from linkedin.django_settings import *  # noqa: E402,F403

DATABASES = {'default': {
    'ENGINE': 'django.db.backends.postgresql',
    'NAME': 'campaign_qa', 'USER': 'campaign_qa', 'PASSWORD': '',
    'HOST': str(socket_dir), 'PORT': '5432', 'CONN_MAX_AGE': 0,
    'TEST': {'NAME': 'test_campaign_qa'},
}}
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
