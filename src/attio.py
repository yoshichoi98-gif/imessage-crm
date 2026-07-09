"""Attio REST client: People pagination, thread upsert, and 429 backoff.

A standalone API-key client (not the OAuth/MCP connection) so it can run headless
under launchd. Reads ATTIO_API_KEY / ATTIO_OBJECT_SLUG from the environment.
"""
import os
import random
import time
import warnings

import requests

warnings.filterwarnings("ignore")  # silence LibreSSL urllib3 NotOpenSSLWarning

BASE = "https://api.attio.com/v2"
PEOPLE_PAGE = 500
MAX_RETRIES = 6


class AttioError(RuntimeError):
    pass


class Attio:
    def __init__(self, api_key=None, object_slug=None):
        self.api_key = api_key or os.environ["ATTIO_API_KEY"]
        self.object_slug = object_slug or os.environ.get("ATTIO_OBJECT_SLUG", "text_threads")
        self.s = requests.Session()
        self.s.headers.update(
            {"Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"}
        )

    # ---- low-level with backoff -------------------------------------------- #
    def _request(self, method, path, **kw):
        url = BASE + path
        for attempt in range(MAX_RETRIES):
            r = self.s.request(method, url, **kw)
            if r.status_code == 429:
                # Exponential backoff with jitter; honor Retry-After when given.
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else (2 ** attempt) + random.random()
                time.sleep(delay)
                continue
            if not r.ok:
                raise AttioError("%s %s -> %s: %s" % (method, path, r.status_code, r.text[:400]))
            return r.json() if r.content else {}
        raise AttioError("rate-limited after %d retries: %s %s" % (MAX_RETRIES, method, path))

    # ---- People ------------------------------------------------------------ #
    def iter_people(self):
        offset = 0
        while True:
            page = self._request(
                "POST", "/objects/people/records/query",
                json={"limit": PEOPLE_PAGE, "offset": offset},
            )["data"]
            if not page:
                return
            for rec in page:
                yield rec
            if len(page) < PEOPLE_PAGE:
                return
            offset += len(page)

    # ---- Curation list ----------------------------------------------------- #
    def list_member_ids(self, list_slug):
        """Return the set of People record_ids that are entries on a list.
        Used to scope the allowlist to a curated 'Text Sync' list."""
        ids = set()
        offset = 0
        while True:
            page = self._request(
                "POST", "/lists/%s/entries/query" % list_slug,
                json={"limit": PEOPLE_PAGE, "offset": offset},
            )["data"]
            if not page:
                break
            for e in page:
                rid = e.get("parent_record_id")
                if rid:
                    ids.add(rid)
            if len(page) < PEOPLE_PAGE:
                break
            offset += len(page)
        return ids

    # ---- Thread record ----------------------------------------------------- #
    def get_thread(self, conversation_key):
        """Return the existing Text Threads record for this key, or None."""
        data = self._request(
            "POST", "/objects/%s/records/query" % self.object_slug,
            json={"filter": {"conversation_key": conversation_key}, "limit": 1},
        )["data"]
        return data[0] if data else None

    def assert_thread(self, conversation_key, attributes):
        """Idempotent upsert of a Text Threads record, matching on
        conversation_key. Creates if absent, updates in place if present."""
        payload = {"data": {"values": attributes}}
        return self._request(
            "PUT",
            "/objects/%s/records?matching_attribute=conversation_key" % self.object_slug,
            json=payload,
        )

    # ---- Notes (transcript lives here) ------------------------------------- #
    def create_note(self, parent_object, parent_record_id, title, content, fmt="plaintext"):
        """Create a Note on a record and return its note_id. fmt is
        'plaintext' or 'markdown'."""
        data = self._request("POST", "/notes", json={"data": {
            "parent_object": parent_object,
            "parent_record_id": parent_record_id,
            "title": title,
            "format": fmt,
            "content": content,
        }})["data"]
        return data["id"]["note_id"]

    def delete_note(self, note_id):
        """Delete a Note. Returns False if it was already gone (404), else True."""
        try:
            self._request("DELETE", "/notes/%s" % note_id)
            return True
        except AttioError as exc:
            if "404" in str(exc):
                return False
            raise
