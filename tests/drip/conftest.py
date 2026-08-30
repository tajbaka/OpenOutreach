from copy import deepcopy

import pytest


@pytest.fixture
def valid_drip_payload():
    return {
        "schema_version": 1,
        "campaign_key": "fedramp_reengagement",
        "name": "FedRAMP re-engagement",
        "audiences": {
            "CSPs": {
                "themes": [
                    {
                        "key": "visibility_gap",
                        "intent": "Explain why continuous visibility matters.",
                        "senders": {
                            "Arian": {
                                "linkedin": [
                                    {
                                        "delay_days": 0,
                                        "body": "Hi {first_name}, visibility matters at {company_name}.",
                                    },
                                    {
                                        "delay_days": 2.5,
                                        "body": "One more thought, {first_name}.",
                                    },
                                ],
                                "gmail": [
                                    {
                                        "delay_days": 1,
                                        "subject": "A question about {company_name}",
                                        "body": "Hi {first_name},\n\nEmail rendition.",
                                    },
                                    {
                                        "delay_days": 3,
                                        "body": "Following up in the same thread.",
                                    },
                                ],
                            },
                        },
                    },
                    {
                        "key": "proof",
                        "intent": "Offer concrete proof.",
                        "senders": {
                            "Arian": {
                                "linkedin": [
                                    {
                                        "delay_days": 0,
                                        "body": "A proof point for {company_name}.",
                                    },
                                ],
                                "gmail": [
                                    {
                                        "delay_days": 0,
                                        "subject": "A question about {company_name}",
                                        "body": "A proof point in the existing enrollment thread.",
                                    },
                                ],
                            },
                        },
                    },
                ],
            },
        },
    }


@pytest.fixture
def second_drip_payload(valid_drip_payload):
    payload = deepcopy(valid_drip_payload)
    payload["campaign_key"] = "second_campaign"
    payload["name"] = "Second campaign"
    return payload
