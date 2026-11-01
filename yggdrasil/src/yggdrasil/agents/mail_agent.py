"""Mail Agent — read your email by voice, through Thunderbird.

"Open my mail and read my first message" → shows the newest inbox message in Thunderbird and reads
it aloud. "Read the next one", "do I have any new mail?", "read the email from Anna".

How it works: Thunderbird already holds the user's accounts (Gmail etc.) and keeps a full-text
search index of every message it has synced — ``global-messages-db.sqlite`` ("Gloda") in the
profile. We READ that index: sender, subject, date, read/starred flags and the plain-text body are
all in there, so no credentials, no IMAP, no driving the GUI. To SHOW a message we hand Thunderbird
a ``mid:<Message-ID>`` URL, which opens that exact message (and marks it read, as a click would).

Thunderbird holds an EXCLUSIVE lock on the index while running, so a normal read-only open fails
with "database is locked". We open it as an immutable snapshot instead (no locking), fresh on
every request, and retry once if we catch it mid-write.
"""
from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.permissions import Capability
from .base import BaseAgent

_HOME = Path.home()
# Where each packaging of Thunderbird keeps profiles.ini (deb/tarball, snap, flatpak, newer XDG).
_PROFILE_ROOTS = (
    _HOME / ".thunderbird",
    _HOME / "snap/thunderbird/common/.thunderbird",
    _HOME / "snap/thunderbird/current/.config/thunderbird",
    _HOME / "snap/thunderbird/current/.thunderbird",
    _HOME / ".config/thunderbird",
    _HOME / ".var/app/org.mozilla.Thunderbird/.thunderbird",
    _HOME / ".var/app/org.mozilla.Thunderbird/config/thunderbird",
)
_INDEX = "global-messages-db.sqlite"

_ORDINALS = {
    "first": 1, "1st": 1, "latest": 1, "newest": 1, "last": 1, "top": 1, "most recent": 1,
    "recent": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4,
    "fifth": 5, "5th": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_SPEAK_BODY_CHARS = 700  # read the gist aloud; the full message is on screen


def find_profile() -> Path | None:
    """The Thunderbird profile directory that has a search index, or None."""
    env = os.environ.get("YGGDRASIL_THUNDERBIRD_PROFILE")
    if env:
        p = Path(env).expanduser()
        return p if (p / _INDEX).exists() else None
    for root in _PROFILE_ROOTS:
        ini = root / "profiles.ini"
        if not ini.exists():
            continue
        cp = configparser.ConfigParser()
        try:
            cp.read(ini, encoding="utf-8")
        except Exception:
            continue
        ordered: list[Path] = []
        # [Install…] Default= is what Thunderbird actually launches; then Default=1; then the rest.
        for sec in cp.sections():
            if sec.startswith("Install") and cp[sec].get("Default"):
                ordered.append(root / cp[sec]["Default"])
        profs = [cp[s] for s in cp.sections() if s.startswith("Profile") and s in cp]
        profs.sort(key=lambda s: s.get("Default") != "1")
        for s in profs:
            path = s.get("Path", "")
            if path:
                ordered.append(root / path if s.get("IsRelative", "1") == "1" else Path(path))
        for p in ordered:
            if (p / _INDEX).exists():
                return p
    return None


def _clean_author(raw: str) -> str:
    """'Example Team <team@example.com> undefined' -> 'Example Team'."""
    a = re.sub(r"\s*\bundefined\b\s*$", "", raw or "").strip()
    m = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', a)
    if m:
        return m.group(1).strip()
    m = re.search(r"<([^>]+)>", a)
    return (m.group(1) if m else a) or "an unknown sender"


def _clean_body(body: str) -> str:
    """Speakable text: drop quoted replies and their 'On … wrote:' header, signatures' trailing
    noise, and bare URLs; collapse whitespace."""
    lines = []
    for line in (body or "").splitlines():
        s = line.strip()
        if s.startswith(">"):
            continue
        if re.match(r"^On .{5,120} wrote:$", s) or s in ("-- ", "--") or \
                re.match(r"^-{2,}\s*(?:Original|Forwarded) Message", s, re.I):
            break
        lines.append(s)
    text = " ".join(lines)
    text = re.sub(r"\(\s*<?https?://[^)\s]*>?\s*\)", "", text)  # "[Go to dashboard] (url)" -> label
    text = re.sub(r"\[([^\]]{1,80})\]", r"\1", text)
    text = re.sub(r"<?https?://\S+", "a link", text)
    text = re.sub(r"(?:a link[\s,.]*){2,}", "some links ", text)
    # Newsletter dividers ("-----", "=====", "*****"), often hundreds of chars long, are noise —
    # and a 990-char run with no space also defeated the sentence trimmer (live: one sentence read).
    text = re.sub(r"(?<!\S)[^\w\s]{3,}(?!\S)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _trim(text: str, limit: int = _SPEAK_BODY_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 3 else cut.rsplit(" ", 1)[0] + "…"), True


def _when(us: int, now: datetime | None = None) -> str:
    """Gloda dates are microseconds since the epoch."""
    try:
        d = datetime.fromtimestamp(us / 1_000_000)
    except Exception:
        return ""
    now = now or datetime.now()
    hm = d.strftime("%H:%M")
    delta = (now.date() - d.date()).days
    if delta == 0:
        return f"today at {hm}"
    if delta == 1:
        return f"yesterday at {hm}"
    if 1 < delta < 7:
        return f"on {d.strftime('%A')}"
    return f"on {d.day} {d.strftime('%B')}" + (f" {d.year}" if d.year != now.year else "")


class MailIndex:
    """Read-only view over Thunderbird's Gloda index (inbox folders of every account)."""

    def __init__(self, profile: Path) -> None:
        self.db = profile / _INDEX

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db}?immutable=1", uri=True, timeout=2)

    def _query(self, fn):
        last: Exception | None = None
        for _ in range(3):  # a torn snapshot while Thunderbird writes is rare and transient
            try:
                con = self._connect()
                try:
                    return fn(con)
                finally:
                    con.close()
            except sqlite3.DatabaseError as e:
                last = e
                time.sleep(0.2)
        raise last  # type: ignore[misc]

    @staticmethod
    def _attr_ids(con) -> dict[str, str]:
        rows = con.execute("SELECT id, name FROM attributeDefinitions "
                           "WHERE extensionName='built-in' AND name IN ('read','star')").fetchall()
        return {name: str(i) for i, name in rows}

    def messages(self, limit: int = 50, unread_only: bool = False, sender: str = "") -> list[dict]:
        """Newest-first inbox messages."""
        def run(con):
            ids = self._attr_ids(con)
            folders = [r[0] for r in con.execute(
                "SELECT id FROM folderLocations WHERE lower(name)='inbox'").fetchall()]
            if not folders:
                return []
            sql = ("SELECT m.id, m.date, m.headerMessageID, m.jsonAttributes, "
                   "t.c0body, t.c1subject, t.c3author "
                   "FROM messages m JOIN messagesText_content t ON t.docid = m.id "
                   f"WHERE m.deleted = 0 AND m.folderID IN ({','.join('?' * len(folders))})")
            args: list[Any] = list(folders)
            if sender:
                sql += " AND lower(t.c3author) LIKE ?"
                args.append(f"%{sender.lower()}%")
            if unread_only and "read" in ids:
                sql += f" AND coalesce(json_extract(m.jsonAttributes, '$.\"{ids['read']}\"'), 0) IN (0, 'false')"
            sql += " ORDER BY m.date DESC LIMIT ?"
            args.append(limit)
            out = []
            for mid, date, hid, attrs, body, subj, author in con.execute(sql, args):
                try:
                    a = json.loads(attrs or "{}")
                except Exception:
                    a = {}
                out.append({
                    "id": mid, "date": date or 0, "message_id": hid or "",
                    "subject": (subj or "").strip() or "no subject",
                    "author": _clean_author(author or ""),
                    "body": body or "",
                    "read": bool(a.get(ids.get("read", ""), False)),
                    "starred": bool(a.get(ids.get("star", ""), False)),
                })
            return out
        return self._query(run)

    def unread_count(self) -> int:
        def run(con):
            ids = self._attr_ids(con)
            if "read" not in ids:
                return 0
            return con.execute(
                "SELECT count(*) FROM messages WHERE deleted = 0 AND folderID IN "
                "(SELECT id FROM folderLocations WHERE lower(name)='inbox') AND "
                f"coalesce(json_extract(jsonAttributes, '$.\"{ids['read']}\"'), 0) IN (0, 'false')"
            ).fetchone()[0]
        return self._query(run)


class MailAgent(BaseAgent):
    domain = "mail"
    module_id = "core.mail"
    planner_examples = [
        'open my mail -> {"steps":[{"action":"mail.open","argument":""}]}',
        'open my email and read my first message -> {"steps":[{"action":"mail.read","argument":"1","argument2":"show"}]}',
        'read my latest email -> {"steps":[{"action":"mail.read","argument":"1"}]}',
        'read the next email -> {"steps":[{"action":"mail.read","argument":"next"}]}',
        'do I have any new mail -> {"steps":[{"action":"mail.list","argument":"unread"}]}',
        'read the email from anna -> {"steps":[{"action":"mail.read","argument":"from:anna"}]}',
    ]
    capabilities = {
        "open": Capability("open", False, "Open Thunderbird, or show the current message in it"),
        "read": Capability("read", False, "Read an inbox message aloud"),
        "list": Capability("list", False, "Summarize recent or unread inbox messages"),
    }

    def __init__(self, bus, perms, profile: Path | None = None) -> None:
        super().__init__(bus, perms)
        self._profile = profile
        self._cursor = 0          # 1-based position of the message last read (0 = none yet)
        self._current: dict | None = None
        self._sender = ""         # "from:X" filter the cursor walks within ("read the next one")
        self._unread = False      # ...and whether it walks unread mail only (after "any new mail?")

    # -- plumbing ---------------------------------------------------------------------------
    def _index(self) -> MailIndex | None:
        prof = self._profile or find_profile()
        return MailIndex(prof) if prof else None

    @staticmethod
    def _has_display() -> bool:
        return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))

    @staticmethod
    def _thunderbird() -> str | None:
        return shutil.which("thunderbird") or shutil.which("thunderbird-esr")

    def _spawn(self, *args: str) -> bool:
        tb = self._thunderbird()
        if not tb or not self._has_display():
            return False
        try:
            subprocess.Popen([tb, *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except Exception:
            return False

    def _show(self, msg: dict) -> bool:
        return bool(msg.get("message_id")) and self._spawn(f"mid:{msg['message_id']}")

    # -- verbs ------------------------------------------------------------------------------
    async def _execute(self, verb: str, params: dict[str, Any]) -> Any:
        arg = (params.get("argument") or "").strip().lower()
        show = params.get("show") or (params.get("argument2") or "").strip().lower() == "show"
        if verb == "open":
            return self._open(arg)
        idx = self._index()
        if idx is None:
            return {"speech": "I can't find your Thunderbird mail. Open Thunderbird once and let it "
                              "finish syncing, and make sure “Enable Global Search and Indexer” is "
                              "on in its settings — that's what lets me read your messages.",
                    "missing": True}
        if verb == "read":
            return self._read(idx, arg, bool(show))
        if verb == "list":
            return self._list(idx, arg)
        raise ValueError(f"unhandled verb '{verb}'")

    def _open(self, arg: str) -> dict:
        if not self._thunderbird():
            return {"speech": "Thunderbird isn't installed.", "assist": True}
        if not self._has_display():
            return {"speech": "I can only open your mail when you're signed in at the desktop."}
        if arg in ("current", "it", "that", "this") and self._current:
            self._show(self._current)
            return {"speech": f"Here's the email from {self._current['author']}.", "brief": "Opening."}
        self._spawn("-mail")
        return {"speech": "Opening your mail.", "brief": "Opening."}

    def _read(self, idx: MailIndex, arg: str, show: bool) -> dict:
        unread = "unread" in arg or arg in ("new", "new one")
        sender = ""
        m = re.search(r"from[: ]\s*(.+)$", arg)
        if m:
            sender = m.group(1).strip(" .?!")
            sender = re.sub(r"^(?:the|my)\s+", "", sender)
        follow = not unread and not sender
        if follow and arg in ("next", "next one"):
            pos, sender, unread = self._cursor + 1, self._sender, self._unread
        elif follow and arg in ("previous", "previous one", "back"):
            pos, sender, unread = max(1, self._cursor - 1), self._sender, self._unread
        elif follow and arg in ("again", "current", "it", "that", "this"):
            pos, sender, unread = max(1, self._cursor), self._sender, self._unread
        else:
            if follow and re.search(r"\bone\b", arg):  # "the second one" -> within the list just heard
                sender, unread = self._sender, self._unread
            pos = 1
            dm = re.search(r"\b(\d+)\b", arg)
            if dm:
                pos = max(1, int(dm.group(1)))
            else:
                for word, n in sorted(_ORDINALS.items(), key=lambda kv: -len(kv[0])):
                    if re.search(rf"\b{re.escape(word)}\b", arg):
                        pos = n
                        break
        msgs = idx.messages(limit=pos, unread_only=unread, sender=sender)
        if len(msgs) < pos:
            if unread and not msgs:
                return {"speech": "You have no unread email in your inbox."}
            if sender and not msgs:
                return {"speech": f"I couldn't find any email from {sender} in your inbox.",
                        "missing": True}
            if not msgs:
                return {"speech": "Your inbox is empty."}
            return {"speech": "That's the end of your inbox — there are no more messages."}
        msg = msgs[pos - 1]
        self._cursor, self._current = pos, msg
        self._sender, self._unread = sender, unread
        shown = self._show(msg) if show else False
        body, cut = _trim(_clean_body(msg["body"]))
        which = {1: "Your newest email", 2: "Your second email", 3: "Your third email"}.get(
            pos, f"Email number {pos}")
        if unread:
            which = "Your newest unread email" if pos == 1 else f"Unread email number {pos}"
        if sender:
            which = f"The latest email from {sender}" if pos == 1 else f"Email {pos} from {sender}"
        when = _when(msg["date"])
        parts = [f"{which} is from {msg['author']}{', ' + when if when else ''}. "
                 f"Subject: {msg['subject']}."]
        parts.append(body if body else "It has no text in the body.")
        if cut:
            parts.append("That's the gist — " + ("the rest is on screen." if shown else
                         "say “open it” to see the whole message."))
        elif not shown:
            parts.append("Say “next” for the next one, or “open it” to see it.")
        return {"speech": " ".join(parts), "subject": msg["subject"], "author": msg["author"]}

    def _list(self, idx: MailIndex, arg: str) -> dict:
        unread_total = idx.unread_count()
        unread = arg != "recent"
        msgs = idx.messages(limit=5, unread_only=unread)
        if unread and not msgs:
            return {"speech": "No new mail — your inbox is all read."}
        self._cursor, self._current, self._sender, self._unread = 0, None, "", unread
        lead = (f"You have {unread_total} unread email{'s' if unread_total != 1 else ''}."
                if unread else "Your latest emails:")
        items = "; ".join(f"{i + 1}, from {m['author']}: {m['subject']}" for i, m in enumerate(msgs))
        tail = " Say “read my first unread email” to hear one." if unread else \
            " Say “read the first one” to hear it."
        return {"speech": f"{lead} The newest: {items}.{tail}"}
