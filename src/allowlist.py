"""Build the allowlist from Attio People.

The allowlist IS the privacy boundary: only conversations whose number matches a
known Attio Person are ever transmitted. We pull every Person, read all
phone-number attributes, normalize to E.164, and return {e164: person_record_id}.
"""
from . import normalize

# Match strictly on the Mobile Phone Number field (Yoshi's chosen join key).
# A texter is linked to a Person only if their number is in this field.
PHONE_SLUGS = ("mobile_phone_number_7",)


def build(client, member_ids=None):
    """Return (allow, collisions) where allow maps E.164 -> contact dict
    {person_id, name, company_id} and collisions counts numbers claimed by more
    than one Person (we keep the most recently updated and log the count).

    If member_ids is given, only People whose record_id is in that set are
    considered (scopes capture to the curated 'Text Sync' list)."""
    allow = {}
    seen_updated = {}  # e164 -> updated_at, for deterministic collision handling
    collisions = 0

    for rec in client.iter_people():
        rid = rec["id"]["record_id"]
        if member_ids is not None and rid not in member_ids:
            continue
        values = rec["values"]
        updated = _record_updated_at(values)
        contact = {"person_id": rid, "name": _full_name(values), "company_id": _company_id(values)}
        for slug in PHONE_SLUGS:
            for pv in values.get(slug, []):
                raw = pv.get("phone_number") or pv.get("original_phone_number")
                e164 = normalize.to_e164(raw)
                if not e164:
                    continue
                if e164 in allow and allow[e164]["person_id"] != rid:
                    collisions += 1
                    if updated <= seen_updated.get(e164, ""):
                        continue  # keep the most recently updated Person
                allow[e164] = contact
                seen_updated[e164] = updated
    return allow, collisions


def _full_name(values):
    name = values.get("name") or []
    if name and isinstance(name[0], dict):
        return name[0].get("full_name") or name[0].get("value")
    return None


def _company_id(values):
    comp = values.get("company") or []
    if comp and isinstance(comp[0], dict):
        return comp[0].get("target_record_id")
    return None


def _record_updated_at(values):
    """Best-effort 'last updated' string for tie-breaking; empty if unknown."""
    for key in ("last_interaction", "created_at"):
        v = values.get(key)
        if v and isinstance(v, list) and v:
            # interaction/timestamp values carry the date under varying keys
            d = v[0]
            return d.get("interacted_at") or d.get("value") or ""
    return ""
