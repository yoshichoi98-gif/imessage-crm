"""Phone-handle -> E.164 normalization.

The allowlist match key and the Attio `conversation_key` are both the contact's
normalized E.164 number, so every handle that flows through the sync passes
through here exactly once.
"""
import phonenumbers

# iMessage handles arrive bare ("(415) 555-1212", "4155551212") with no country
# code for domestic numbers, so phonenumbers needs a default region to anchor them.
DEFAULT_REGION = "US"


def to_e164(handle, default_region=DEFAULT_REGION):
    """Return the E.164 string for a phone handle, or None if it isn't a valid
    phone number (e.g. an iMessage email handle, or an unparseable string)."""
    if not handle:
        return None
    handle = handle.strip()
    if "@" in handle:
        return None  # email-based iMessage handle; v1 keys on phone only
    try:
        num = phonenumbers.parse(handle, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
