"""iMessage -> Attio sync orchestration.

Per run: snapshot chat.db, refresh the Attio allowlist, find 1:1 conversations
with new messages, drop any number not on the allowlist (the privacy boundary),
rebuild each surviving transcript from source, and assert one Text Threads
record per conversation. The ROWID watermark advances only after all asserts
succeed, so a mid-run failure safely reprocesses next time.

Usage:
    python -m src.main --once            # one sync pass
    python -m src.main --once --since 2026-06-11
    python -m src.main --once --dry-run  # no Attio writes; print what would happen
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta

from . import allowlist as allowlist_mod
from . import chatdb
from .attio import Attio

STATE_DIR = os.path.expanduser("~/.imessage-attio")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
ENV_PATH = os.path.join(STATE_DIR, "env")
ALLOWLIST_CACHE = os.path.join(STATE_DIR, "allowlist_cache.json")
ALLOWLIST_TTL = 900  # refresh the Attio contact lookup at most once per 15 min


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #
def load_env(path=ENV_PATH):
    """Load KEY=VALUE lines from the secrets file into os.environ (launchd does
    not source it for us)."""
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            return json.load(fh)
    return {"last_rowid": 0}


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, STATE_PATH)


def load_or_build_allowlist(client, sync_list, ttl=ALLOWLIST_TTL):
    """Return (allow, collisions, cached). The Attio People + list lookup is the
    bulk of each run's API calls but rarely changes, so we cache the built
    allowlist on disk and only rebuild it once per `ttl` seconds. The cache is
    keyed by sync_list so changing the curation list invalidates it."""
    now = _now()
    if os.path.exists(ALLOWLIST_CACHE):
        try:
            with open(ALLOWLIST_CACHE) as fh:
                c = json.load(fh)
            if c.get("sync_list") == (sync_list or "") and (now - c.get("built_at", 0)) < ttl:
                return c["allow"], c.get("collisions", 0), True
        except Exception:  # noqa: BLE001 - a bad cache just forces a rebuild
            pass

    member_ids = client.list_member_ids(sync_list) if sync_list else None
    allow, collisions = allowlist_mod.build(client, member_ids=member_ids)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = ALLOWLIST_CACHE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"built_at": now, "sync_list": sync_list or "",
                       "allow": allow, "collisions": collisions}, fh)
        os.chmod(tmp, 0o600)  # holds contact names/numbers; keep owner-only
        os.replace(tmp, ALLOWLIST_CACHE)
    except Exception:  # noqa: BLE001 - failing to cache is non-fatal
        pass
    return allow, collisions, False


# --------------------------------------------------------------------------- #
# Attio value formatting
# --------------------------------------------------------------------------- #
def iso(unix_seconds):
    return datetime.fromtimestamp(unix_seconds).astimezone().isoformat()


def ref(object_slug, record_id):
    return [{"target_object": object_slug, "target_record_id": record_id}]


def build_attributes(e164, contact, transcript, note_id=None):
    """Structured metadata for the Text Threads record. The transcript itself
    lives in a Note on the Person; we only store its id here for replacement."""
    name = contact.get("name") or e164
    label = "%s ↔ %s" % (SELF_LABEL, name)
    attrs = {
        "thread_label": label,
        "conversation_key": e164,
        "contact": ref("people", contact["person_id"]),
        "channel": transcript["channel"],
        "first_message_at": iso(transcript["first_message_at"]),
        "last_message_at": iso(transcript["last_message_at"]),
        "message_count": transcript["message_count"],
    }
    if contact.get("company_id"):
        attrs["company"] = ref("companies", contact["company_id"])
    if note_id:
        attrs["transcript_note_id"] = note_id
    return attrs


def existing_note_id(record):
    if not record:
        return None
    v = record["values"].get("transcript_note_id")
    return v[0]["value"] if v else None


SELF_LABEL = "Me"


def note_title(contact, e164):
    name = contact.get("name") or "Unknown"
    return "SMS - %s - %s" % (name, e164)


# --------------------------------------------------------------------------- #
# Sync
# --------------------------------------------------------------------------- #
def run(args):
    global SELF_LABEL
    SELF_LABEL = args.self_label

    since_unix = None
    if args.since:
        since_unix = datetime.strptime(args.since, "%Y-%m-%d").timestamp()

    state = load_state()
    last_rowid = 0 if args.reprocess else state.get("last_rowid", 0)

    client = None if args.dry_run else Attio()
    # On a dry run we still need the allowlist; use a client just for reads.
    read_client = client or Attio()

    # Scope capture to a curated list if one is configured (ATTIO_SYNC_LIST).
    # The lookup is cached for ALLOWLIST_TTL to keep idle runs cheap.
    sync_list = os.environ.get("ATTIO_SYNC_LIST")
    allow, collisions, cached = load_or_build_allowlist(read_client, sync_list)
    log("allowlist: %d contact(s)%s%s" % (
        len(allow),
        " scoped to '%s'" % sync_list if sync_list else "",
        " (cached)" if cached else " (refreshed from Attio)"))
    if collisions:
        log("note: %d phone-number collision(s) across People; kept most recent" % collisions)

    tmpdir, db = chatdb.snapshot()
    try:
        con = chatdb.connect_ro(db)
        chat_map = chatdb.one_to_one_chats(con)
        new_max = chatdb.max_rowid(con)
        changed = chatdb.changed_e164s(con, last_rowid, chat_map, since_unix)

        allowed = sorted(changed & set(allow))
        skipped = len(changed) - len(allowed)

        asserted = errors = 0
        for e164 in allowed:
            contact = allow[e164]
            chat_ids = chatdb.chat_ids_for_e164(e164, chat_map)
            t = chatdb.build_transcript(con, chat_ids, contact.get("name") or e164,
                                        self_label=SELF_LABEL, since_unix=since_unix)
            if not t:
                continue
            if args.dry_run:
                log("DRY  %s  %s  msgs=%d  %s"
                    % (e164, contact.get("name") or "?", t["message_count"], t["channel"]))
                asserted += 1
                continue
            try:
                # Notes can't be edited via API, so keep one per conversation by
                # creating the fresh note, pointing the record at it, then
                # deleting the previous note. Record always points at a live note.
                old_note = existing_note_id(client.get_thread(e164))
                new_note = client.create_note("people", contact["person_id"],
                                              note_title(contact, e164),
                                              t["note_markdown"], fmt="markdown")
                client.assert_thread(e164, build_attributes(e164, contact, t, note_id=new_note))
                if old_note and old_note != new_note:
                    client.delete_note(old_note)
                asserted += 1
                log("sync %s  %s  msgs=%d" % (e164, contact.get("name") or "?", t["message_count"]))
            except Exception as exc:  # noqa: BLE001 - log and keep going
                errors += 1
                log("ERROR asserting %s: %s" % (e164, exc))

        con.close()

        # Advance watermark only if nothing errored (and not a dry run / since-bounded peek).
        if not args.dry_run and errors == 0 and not args.since and not args.reprocess:
            state["last_rowid"] = new_max
            state["last_run"] = iso(_now())
            save_state(state)

        log("done: %d conversation(s) with new messages, %d allowlisted, %d asserted, "
            "%d skipped (not on allowlist), %d error(s)"
            % (len(changed), len(allowed), asserted, skipped, errors))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _now():
    return datetime.now().timestamp()


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def main(argv=None):
    load_env()  # before argparse so SELF_LABEL default can come from the env file
    p = argparse.ArgumentParser(description="iMessage -> Attio conversation sync")
    p.add_argument("--once", action="store_true", help="run a single sync pass")
    p.add_argument("--dry-run", action="store_true", help="no Attio writes; print plan")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only messages on/after this date")
    p.add_argument("--self-label", default=os.environ.get("SELF_LABEL", "Me"),
                   help="label for outbound (your) messages; or set SELF_LABEL in env")
    p.add_argument("--reprocess", action="store_true",
                   help="ignore the watermark and re-examine all history (does not advance it)")
    args = p.parse_args(argv)

    if "ATTIO_API_KEY" not in os.environ:
        sys.exit("ATTIO_API_KEY not set (expected in %s)" % ENV_PATH)
    run(args)


if __name__ == "__main__":
    main()
