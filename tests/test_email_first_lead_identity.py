import pytest
from django.db import IntegrityError, transaction

from crm.models import Lead


pytestmark = pytest.mark.django_db


def test_multiple_email_first_contacts_can_exist_without_linkedin_urls():
    first = Lead.objects.create(email="first@example.com")
    second = Lead.objects.create(email="second@example.com")

    assert first.linkedin_url == ""
    assert second.linkedin_url == ""


def test_nonblank_linkedin_identity_remains_unique():
    url = "https://www.linkedin.com/in/exact-contact/"
    Lead.objects.create(linkedin_url=url)

    with pytest.raises(IntegrityError), transaction.atomic():
        Lead.objects.create(linkedin_url=url)
