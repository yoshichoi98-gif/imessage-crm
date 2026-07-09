"""Read layer for the macOS Messages database (~/Library/Messages/chat.db).

Handles the three things that break naive implementations:
  1. message.text is frequently NULL on modern macOS; the body lives in the
     `attributedBody` typedstream blob, which we decode here.
  2. Apple Cocoa timestamps (nanoseconds since 2001-01-01) -> unix.
  3. The live DB holds WAL locks, so we read a snapshot copy, never the original.

Conversations are keyed by the other party's normalized E.164 number. A single
contact may own several `handle` rows (iMessage vs SMS, differently-formatted
numbers); we group those handles together so one person == one thread.
"""
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime

import typedstream
from typedstream.archiving import TypedValue

from . import normalize

DEFAULT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# Cocoa/Core-Data epoch (2001-01-01 UTC) offset from the unix epoch.
COCOA_EPOCH_OFFSET = 978307200


# --------------------------------------------------------------------------- #
# Snapshot
# --------------------------------------------------------------------------- #
def snapshot(db_path=DEFAULT_DB_PATH):
    """Copy chat.db (+ wal/shm) to a temp dir and return (tempdir, db_copy_path).

    Caller is responsible for shutil.rmtree(tempdir) when done.
    """
    tmpdir = tempfile.mkdtemp(prefix="imessage_attio_")
    for ext in ("", "-wal", "-shm"):
        src = db_path + ext
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(tmpdir, "chat.db" + ext))
    return tmpdir, os.path.join(tmpdir, "chat.db")


def connect_ro(db_copy_path):
    return sqlite3.connect("file:%s?mode=ro" % db_copy_path, uri=True)


# --------------------------------------------------------------------------- #
# Field decoders
# --------------------------------------------------------------------------- #
def decode_attributed_body(blob):
    """Extract the message string from an `attributedBody` typedstream blob.

    Returns the text, or None if the blob holds no string (e.g. attachment-only
    messages) or fails to decode.
    """
    if blob is None:
        return None
    try:
        obj = typedstream.unarchive_from_data(bytes(blob))
    except Exception:
        return None
    contents = getattr(obj, "contents", None)
    if not contents:
        return None
    for item in contents:
        val = item.value if isinstance(item, TypedValue) else item
        inner = getattr(val, "value", None)
        if isinstance(inner, str) and inner:
            return inner
    return None


# U+FFFC OBJECT REPLACEMENT CHARACTER: iMessage inserts one wherever an inline
# attachment sits in the body. A body that is only these is attachment-only.
OBJ_REPLACEMENT = "￼"


def message_body(text, attributed_body, has_attachment):
    """Best available readable body for a message row, with an attachment
    placeholder when there's no text but media is present."""
    body = text or decode_attributed_body(attributed_body) or ""
    stripped = body.replace(OBJ_REPLACEMENT, "").strip()
    if stripped:
        return stripped
    if has_attachment or OBJ_REPLACEMENT in body:
        return "[attachment]"
    return ""


def cocoa_to_unix(date):
    """Convert a message.date value to unix seconds.

    Modern macOS stores nanoseconds since the Cocoa epoch; legacy DBs store
    seconds. Guard on magnitude to handle both.
    """
    if date is None:
        return None
    if date < 1e11:  # looks like seconds
        return date + COCOA_EPOCH_OFFSET
    return date / 1_000_000_000 + COCOA_EPOCH_OFFSET


def fmt_ts(unix_seconds):
    if unix_seconds is None:
        return ""
    return datetime.fromtimestamp(unix_seconds).strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------- #
# Conversation discovery
# --------------------------------------------------------------------------- #
def one_to_one_chats(con):
    """Return {chat_id: participant_handle_string} for every 1:1 chat.

    A chat is 1:1 when it has exactly one participant in chat_handle_join. Group
    chats (>1 participant) are excluded entirely in v1.
    """
    rows = con.execute(
        """
        SELECT chj.chat_id, h.id
        FROM chat_handle_join chj
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE chj.chat_id IN (
            SELECT chat_id FROM chat_handle_join
            GROUP BY chat_id HAVING COUNT(*) = 1
        )
        """
    ).fetchall()
    return {chat_id: handle for chat_id, handle in rows}


def max_rowid(con):
    row = con.execute("SELECT MAX(ROWID) FROM message").fetchone()
    return row[0] or 0


def changed_e164s(con, last_rowid, chat_map, since_unix=None):
    """Set of normalized E.164 numbers whose 1:1 thread has messages newer than
    last_rowid (and, if since_unix is given, on/after that time). Handles that
    don't normalize (emails, junk) are skipped."""
    chat_ids = set(chat_map.keys())
    if not chat_ids:
        return set()
    placeholders = ",".join("?" * len(chat_ids))
    rows = con.execute(
        """
        SELECT DISTINCT cmj.chat_id, m.date
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE m.ROWID > ? AND cmj.chat_id IN (%s)
        """
        % placeholders,
        [last_rowid, *chat_ids],
    ).fetchall()
    out = set()
    for chat_id, date in rows:
        if since_unix is not None and (cocoa_to_unix(date) or 0) < since_unix:
            continue
        e164 = normalize.to_e164(chat_map.get(chat_id, ""))
        if e164:
            out.add(e164)
    return out


def chat_ids_for_e164(e164, chat_map):
    """All 1:1 chat_ids whose participant handle normalizes to this number.
    Covers a contact reachable via both iMessage and SMS, or via differently
    formatted handles."""
    return [cid for cid, handle in chat_map.items() if normalize.to_e164(handle) == e164]


def build_transcript(con, chat_ids, contact_label, self_label="Me", since_unix=None):
    """Rebuild the full chat-formatted transcript for a contact from source.

    Rebuilding from scratch each run (rather than appending) is what makes the
    sync idempotent and dedup-proof. Returns a dict with the transcript text and
    the metadata the Attio record needs. If since_unix is given, messages older
    than that are excluded (bounds the first backfill).
    """
    if not chat_ids:
        return None
    placeholders = ",".join("?" * len(chat_ids))
    rows = con.execute(
        """
        SELECT m.ROWID, m.is_from_me, m.date, m.text, m.attributedBody,
               m.cache_has_attachments, m.service
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE cmj.chat_id IN (%s)
        ORDER BY m.date ASC, m.ROWID ASC
        """
        % placeholders,
        list(chat_ids),
    ).fetchall()

    msgs = []
    first_ts = last_ts = None
    services = {}
    for rowid, is_from_me, date, text, ab, has_att, service in rows:
        unix = cocoa_to_unix(date)
        if since_unix is not None and (unix or 0) < since_unix:
            continue
        body = message_body(text, ab, has_att)
        if not body:
            continue  # nothing readable (rare: empty/system rows)
        if first_ts is None:
            first_ts = unix
        last_ts = unix
        if service:
            services[service] = services.get(service, 0) + 1
        who = self_label if is_from_me else contact_label
        msgs.append({"unix": unix, "from_me": bool(is_from_me), "who": who, "body": body})

    if not msgs:
        return None

    # Channel: iMessage if any message used it, else SMS.
    channel = "iMessage" if services.get("iMessage") else "SMS"
    return {
        "transcript": render_plain(msgs),
        "note_markdown": render_imessage_md(msgs),
        "first_message_at": first_ts,
        "last_message_at": last_ts,
        "message_count": len(msgs),
        "channel": channel,
    }


def render_plain(msgs):
    """Compact one-line-per-message format (used for logs / fallback)."""
    out = []
    for m in msgs:
        arrow = "→" if m["from_me"] else "←"
        out.append("%s [%s] %s: %s" % (arrow, fmt_ts(m["unix"]), m["who"], m["body"]))
    return "\n".join(out)


# Attio caps a note at 3476 "blocks" (each day header / message bubble is one
# block). Keep the most recent blocks under that, with margin.
MAX_NOTE_BLOCKS = 3000


def render_imessage_md(msgs):
    """Render the conversation as iMessage-style markdown for the Attio note:
    date headers, and consecutive messages from one sender stacked under a single
    bubble header (🔵 = you/outbound, ⚪ = the contact/inbound).

    Very long conversations are trimmed to the most recent MAX_NOTE_BLOCKS blocks
    (Attio rejects larger notes); a notice is prepended and the full history still
    lives in Messages. The Text Threads record keeps the true message_count."""
    # Group consecutive messages by (date, sender, direction).
    groups = []
    for m in msgs:
        dt = datetime.fromtimestamp(m["unix"])
        day = dt.strftime("%A, %B %d, %Y")
        tm = dt.strftime("%I:%M %p").lstrip("0")
        if (groups and groups[-1]["day"] == day and groups[-1]["who"] == m["who"]
                and groups[-1]["from_me"] == m["from_me"]):
            groups[-1]["bodies"].append(m["body"])
        else:
            groups.append({"day": day, "who": m["who"], "from_me": m["from_me"],
                           "time": tm, "bodies": [m["body"]]})

    # Order newest-first for CRM scanning: most recent day on top, and the most
    # recent exchange at the top of each day. Lines inside a single bubble stay
    # in natural order so each message still reads correctly.
    days = []  # [(day, [groups...]) ] in chronological order
    for g in groups:
        if not days or days[-1][0] != g["day"]:
            days.append((g["day"], []))
        days[-1][1].append(g)

    out = []
    for day, day_groups in reversed(days):
        out.append("## " + day)
        for g in reversed(day_groups):
            bubble = "🔵" if g["from_me"] else "⚪"
            header = "%s **%s** · %s" % (bubble, g["who"], g["time"])
            body = "  \n".join(g["bodies"])  # two-space hard breaks keep stacked lines distinct
            out.append(header + "  \n" + body)

    if len(out) > MAX_NOTE_BLOCKS:
        # Newest content is at the head, so drop the oldest (tail) and note it.
        out = out[: MAX_NOTE_BLOCKS - 1]
        out.append("*⚠️ This conversation is too long to show in full here — older "
                   "messages are omitted. The complete history remains in Messages.*")
    return "\n\n".join(out).strip()
