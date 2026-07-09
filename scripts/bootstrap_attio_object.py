"""One-time, idempotent bootstrap of the `Text Threads` custom object in Attio.

Safe to re-run: it checks for the object/attribute/option before creating each,
so a second run is a no-op. Reads ATTIO_API_KEY / ATTIO_OBJECT_SLUG from env
(load ~/.imessage-attio/env first).

Usage:  python -m scripts.bootstrap_attio_object
"""
import os
import sys
import warnings

import requests

warnings.filterwarnings("ignore")

BASE = "https://api.attio.com/v2"
OBJECT_SLUG = os.environ.get("ATTIO_OBJECT_SLUG", "text_threads")
H = {"Authorization": "Bearer " + os.environ["ATTIO_API_KEY"], "Content-Type": "application/json"}

# Attribute definitions, in creation order. conversation_key is the unique match
# key the sync upserts against.
ATTRIBUTES = [
    {"title": "Thread Label", "api_slug": "thread_label", "type": "text"},
    {"title": "Conversation Key", "api_slug": "conversation_key", "type": "text", "is_unique": True},
    {"title": "Contact", "api_slug": "contact", "type": "record-reference",
     "config": {"record_reference": {"allowed_objects": ["people"]}}},
    {"title": "Company", "api_slug": "company", "type": "record-reference",
     "config": {"record_reference": {"allowed_objects": ["companies"]}}},
    {"title": "Deal", "api_slug": "deal", "type": "record-reference",
     "config": {"record_reference": {"allowed_objects": ["deals"]}}},
    {"title": "Channel", "api_slug": "channel", "type": "select",
     "_options": ["iMessage", "SMS"]},
    # The readable transcript lives in a Note on the Person, not here. We only
    # track the synced note's id so each run can replace it in place.
    {"title": "Transcript Note ID", "api_slug": "transcript_note_id", "type": "text"},
    {"title": "First Message At", "api_slug": "first_message_at", "type": "timestamp"},
    {"title": "Last Message At", "api_slug": "last_message_at", "type": "timestamp"},
    {"title": "Message Count", "api_slug": "message_count", "type": "number"},
]


def _get(path):
    r = requests.get(BASE + path, headers=H)
    r.raise_for_status()
    return r.json()["data"]


def _post(path, body):
    r = requests.post(BASE + path, headers=H, json={"data": body})
    if not r.ok:
        raise SystemExit("POST %s failed %s: %s" % (path, r.status_code, r.text[:500]))
    return r.json()["data"]


def ensure_object():
    objects = _get("/objects")
    if any(o["api_slug"] == OBJECT_SLUG for o in objects):
        print("object '%s' already exists" % OBJECT_SLUG)
        return
    _post("/objects", {
        "api_slug": OBJECT_SLUG,
        "singular_noun": "Text Thread",
        "plural_noun": "Text Threads",
    })
    print("created object '%s'" % OBJECT_SLUG)


def ensure_attributes():
    existing = {a["api_slug"]: a for a in _get("/objects/%s/attributes" % OBJECT_SLUG)}
    for spec in ATTRIBUTES:
        slug = spec["api_slug"]
        options = spec.pop("_options", None) if "_options" in spec else None
        if slug not in existing:
            body = {
                "description": "",
                "is_required": False,
                "is_unique": False,
                "is_multiselect": False,
                "config": {},
            }
            body.update({k: v for k, v in spec.items() if not k.startswith("_")})
            _post("/objects/%s/attributes" % OBJECT_SLUG, body)
            print("  created attribute '%s' (%s)" % (slug, spec["type"]))
        else:
            print("  attribute '%s' exists" % slug)
        if options:
            ensure_select_options(slug, options)


def ensure_select_options(attr_slug, want):
    have = {o["title"] for o in _get("/objects/%s/attributes/%s/options" % (OBJECT_SLUG, attr_slug))}
    for title in want:
        if title not in have:
            _post("/objects/%s/attributes/%s/options" % (OBJECT_SLUG, attr_slug), {"title": title})
            print("    added option '%s' -> %s" % (title, attr_slug))


def archive_legacy_transcript():
    """Earlier versions stored the transcript in a long-text attribute. The
    transcript now lives in a Note, so archive that attribute if it exists."""
    existing = {a["api_slug"]: a for a in _get("/objects/%s/attributes" % OBJECT_SLUG)}
    a = existing.get("transcript")
    if a and not a.get("is_archived"):
        r = requests.patch(
            BASE + "/objects/%s/attributes/transcript" % OBJECT_SLUG,
            headers=H, json={"data": {"is_archived": True}},
        )
        print("  archived legacy 'transcript' attribute" if r.ok
              else "  (could not archive 'transcript': %s)" % r.status_code)


def main():
    if "ATTIO_API_KEY" not in os.environ:
        sys.exit("ATTIO_API_KEY not set (source ~/.imessage-attio/env first)")
    ensure_object()
    ensure_attributes()
    archive_legacy_transcript()
    print("\nBootstrap complete. Object slug: %s" % OBJECT_SLUG)


if __name__ == "__main__":
    main()
