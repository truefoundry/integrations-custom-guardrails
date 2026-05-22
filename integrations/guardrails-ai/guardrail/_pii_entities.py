"""Shared PII entity allowlist for DetectPII.

Tight list — excludes LOCATION, PERSON, DATE_TIME, URL, NRP, ORGANIZATION,
all of which produce false positives on ordinary prompts. We learned this
during Phase 0: default `pii_entities='pii'` flagged "What is the capital
of France?" as PII because Presidio's LOCATION recognizer fires on country
names.
"""

PII_ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "US_ITIN",
]
