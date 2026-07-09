# iMessage → Attio Conversation Sync

Reads business text conversations from a Mac's local Messages database and
mirrors each one into Attio as a single, continuously-updated record per
conversation (full readable transcript + metadata).

**Observe-only.** It never sends messages. It only writes to Attio.

**Privacy boundary:** a conversation is synced **only** if the other number is in
the **Mobile Phone Number** field of a Person on the curated **Text Sync** list in
Attio. Everyone else — including Attio People not on the list — is filtered out
*before* any network call. Personal texts never leave the machine. Group chats are
skipped in v1.

To include a contact: add them to Attio with their mobile number and drag them
onto the **Text Sync** list. To stop syncing someone: remove them from the list.

---

## How it works

```
Messages app  →  ~/Library/Messages/chat.db  →  sync  →  Attio "Text Threads"
```

Each run snapshots the local DB, pulls the Attio People list to build an
allowlist of known phone numbers, finds 1:1 threads with new messages, drops
anything not on the allowlist, rebuilds each surviving transcript from source,
and upserts one record per conversation (matched on the contact's E.164 number,
so re-runs update in place with zero duplicates).

Runs opportunistically via a launchd agent (on login + every 5 min while awake).
A closed lid means *delayed*, not *lost*: `chat.db` is cumulative and the next
run backfills everything since the last watermark.

---

## Prerequisites

**On the Mac:**
- macOS Ventura (13) or later.
- **Messages in iCloud** enabled (so history is present on the Mac).
- **Text Message Forwarding** to this Mac (iPhone → Settings → Messages → Text
  Message Forwarding) so green-bubble SMS is captured too.
- **Full Disk Access** for the sync. Grant it to `run.sh` *and* the venv Python:
  System Settings → Privacy & Security → Full Disk Access → add
  `<repo>/.venv/bin/python3` (and `<repo>/run.sh`). Without this, reads fail.

**On Attio:**
- Pro/Enterprise plan with admin rights (needed to create a custom object).
- An API key with `record_permission:read-write` and
  `object_configuration:read-write`. Create it in Attio → Settings → Developers.

---

## Setup

**Easy path (new machine, e.g. the CEO's laptop):**

```bash
cd imessage-attio-sync
bash scripts/setup.sh        # prompts for the API key + name, does everything
```

It builds the environment, writes `~/.imessage-attio/env`, ensures the Attio
object exists, installs the background agent, and prints the one manual step it
can't automate: granting **Full Disk Access** (see Prerequisites).

**Manual path:**

```bash
make install                 # venv + deps

# create ~/.imessage-attio/env (chmod 600), containing:
#   ATTIO_API_KEY=...
#   ATTIO_OBJECT_SLUG=text_threads
#   ATTIO_SYNC_LIST=text_sync
#   SELF_LABEL=YourFirstName

make bootstrap               # create the Attio object + attributes (idempotent)
make dry-run                 # see what WOULD sync, no writes
make install-agent           # load the background agent
```

`make sync-now` runs one pass by hand. `make logs` tails the log.

---

## Configuration

Config in `~/.imessage-attio/env`: `ATTIO_API_KEY`, `ATTIO_OBJECT_SLUG`,
`ATTIO_SYNC_LIST` (curation list slug; capture is scoped to its members),
`SELF_LABEL`. Plus command-line flags:

| Flag | Meaning |
|---|---|
| `--once` | run a single pass (what the agent uses) |
| `--dry-run` | no Attio writes; print the plan |
| `--since YYYY-MM-DD` | only messages on/after this date (bounds the first backfill) |
| `--self-label NAME` | label for your outbound messages (default `SELF_LABEL` env, else `Me`) |
| `--reprocess` | ignore the watermark and re-examine all history (doesn't advance it) |

State (the `last_rowid` watermark) lives in `~/.imessage-attio/state.json`.

---

## The Attio object

Custom object `text_threads` ("Text Threads"), one record per conversation:

| Attribute | Type | Notes |
|---|---|---|
| `thread_label` | text | e.g. `Yoshi ↔ Sarah Chen` |
| `conversation_key` | text (unique) | the contact's E.164 number — the upsert match key |
| `contact` | → People | linked Person |
| `company` | → Companies | from the Person's company, when set |
| `deal` | → Deals | reference exists but is not auto-populated in v1 |
| `channel` | select | `iMessage` / `SMS` |
| `transcript` | text | full rolling chat-formatted transcript |
| `first_message_at` / `last_message_at` | timestamp | |
| `message_count` | number | |

---

## v1 scope / known behavior

- **1:1 only.** Group chats are skipped.
- **Phone match only.** Email-based iMessage handles aren't matched in v1.
- **Attachments** show as `[attachment]` placeholders; media isn't ingested.
- **Tapbacks** ("Loved …", "Emphasized …") appear as their own transcript lines.
- **Deleted messages** drop out of the transcript (it's rebuilt from source).
- Transcripts rebuild in full each run — simple and dedup-proof; use `--since`
  to bound very long threads if payloads get large.
- The Attio contact lookup (People + Text Sync list) is cached for 15 minutes
  (`allowlist_cache.json`), so frequent 5-minute runs stay light on the API.
  Rate limits are handled with exponential backoff + jitter on HTTP 429.

---

## Files

```
src/chatdb.py      snapshot + attributedBody decode + transcript build
src/normalize.py   phone → E.164
src/allowlist.py   Attio People → {E.164: contact}
src/attio.py       REST client: people pagination, upsert, 429 backoff
src/main.py        orchestration + watermark
scripts/bootstrap_attio_object.py   one-time object/attribute creation
scripts/install_launchd.sh          install the background agent
```
