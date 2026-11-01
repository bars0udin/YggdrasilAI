"""Mail agent (Thunderbird's Gloda index) + its deterministic route."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime

import pytest

from yggdrasil.agents import mail_agent
from yggdrasil.agents.mail_agent import MailAgent, MailIndex, _clean_author, _clean_body, find_profile
from yggdrasil.core.orchestrator import _mail_route


# --- routing -------------------------------------------------------------------------------
@pytest.mark.parametrize("phrase, verb, arg, show", [
    # the live miss: planned as app.launch(mail) x3
    ("open my mail and read my first message", "read", "first", True),
    ("Open my email and read me the latest one", "read", "latest", True),
    ("read my first email", "read", "first", False),
    ("read my latest email", "read", "latest", False),
    ("what's my newest email", "read", "newest", False),
    ("read the second email", "read", "second", False),
    ("read my 3rd email", "read", "3", False),
    ("read my first unread email", "read", "first unread", False),
    ("read the email from Carol", "read", "from:carol", False),
    ("can you read me the email from Anna please", "read", "from:anna", False),
    ("read the next email", "read", "next", False),
    ("open my first email", "read", "first", True),
])
def test_mail_read_routes(phrase, verb, arg, show):
    r = _mail_route(phrase)
    assert r is not None, phrase
    assert r[0] == verb and r[1]["argument"] == arg and bool(r[1].get("show")) is show


@pytest.mark.parametrize("phrase, verb", [
    ("open my mail", "open"),
    ("open thunderbird", "open"),
    ("open my inbox", "open"),
    ("launch my email", "open"),
    ("check my email", "list"),
    ("do I have any new mail", "list"),
    ("any new emails?", "list"),
    ("how many unread emails do I have", "list"),
])
def test_mail_other_routes(phrase, verb):
    r = _mail_route(phrase)
    assert r is not None and r[0] == verb, phrase


@pytest.mark.parametrize("phrase", [
    "send an email to anna",            # composing isn't this agent's job
    "write an email to my boss",
    "reply to that email",
    "set up my email account",
    "open google and search for cats",
    "open the first link",
    "read this page",
    "next",                             # only a follow-up right after mail
    "open it",
    "what is the weather",
])
def test_mail_route_leaves_others_alone(phrase):
    assert _mail_route(phrase) is None


@pytest.mark.parametrize("phrase, verb, arg", [
    ("next", "read", "next"),
    ("read the next one", "read", "next"),
    ("okay, next one please", "read", "next"),
    ("previous", "read", "previous"),
    ("again", "read", "again"),
    ("open it", "open", "current"),
    ("show me that email", "open", "current"),
    ("the second one", "read", "second one"),
])
def test_mail_follow_ups(phrase, verb, arg):
    r = _mail_route(phrase, follow_up=True)
    assert r == (verb, {"argument": arg})


# --- a fake Gloda index ---------------------------------------------------------------------
def _make_profile(tmp_path, msgs):
    db = sqlite3.connect(tmp_path / "global-messages-db.sqlite")
    db.executescript("""
        CREATE TABLE folderLocations (id INTEGER PRIMARY KEY, folderURI TEXT, dirtyStatus INTEGER,
                                      name TEXT, indexingPriority INTEGER);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, folderID INTEGER, messageKey INTEGER,
            conversationID INTEGER, date INTEGER, headerMessageID TEXT, deleted INTEGER DEFAULT 0,
            jsonAttributes TEXT, notability INTEGER DEFAULT 0);
        CREATE TABLE messagesText_content (docid INTEGER PRIMARY KEY, c0body, c1subject,
            c2attachmentNames, c3author, c4recipients);
        CREATE TABLE attributeDefinitions (id INTEGER PRIMARY KEY, attributeType INTEGER,
            extensionName TEXT, name TEXT, parameter BLOB);
        INSERT INTO attributeDefinitions VALUES (58,1,'built-in','star',NULL),(59,1,'built-in','read',NULL);
        INSERT INTO folderLocations VALUES (1,'imap://x/INBOX',0,'Inbox',50),(2,'imap://x/Sent',0,'Sent Mail',60);
    """)
    now = int(time.time() * 1_000_000)
    for i, (author, subj, body, read, folder, deleted) in enumerate(msgs, start=1):
        db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,0)",
                   (i, folder, i, i, now - i * 60_000_000, f"m{i}@x", deleted,
                    json.dumps({"58": False, "59": read})))
        db.execute("INSERT INTO messagesText_content VALUES (?,?,?,?,?,?)",
                   (i, body, subj, "", author, "me@x"))
    db.commit()
    db.close()
    return tmp_path


MSGS = [  # newest first
    ("Anna Berg <anna@x.com> undefined", "Lunch?", "Are you free at noon?\n\nOn Mon, Bob wrote:\n> old", False, 1, 0),
    ("Bob <bob@x.com> undefined", "Report", "Attached. https://x.com/a https://x.com/b", True, 1, 0),
    ("me <me@x.com> undefined", "Sent thing", "outgoing", True, 2, 0),        # not inbox
    ("Spam <s@x.com> undefined", "Gone", "deleted", False, 1, 1),            # deleted
    ("Anna Berg <anna@x.com> undefined", "Earlier", "First note", True, 1, 0),
]


@pytest.fixture
def agent(tmp_path, monkeypatch):
    prof = _make_profile(tmp_path, MSGS)
    a = MailAgent(None, None, prof)
    shown = []
    monkeypatch.setattr(a, "_spawn", lambda *args: shown.append(args) or True)
    a.shown = shown
    return a


def run(agent, verb, **params):
    return asyncio.run(agent._execute(verb, params))


def test_index_filters_inbox_and_deleted(tmp_path):
    idx = MailIndex(_make_profile(tmp_path, MSGS))
    assert [m["subject"] for m in idx.messages()] == ["Lunch?", "Report", "Earlier"]
    assert [m["subject"] for m in idx.messages(unread_only=True)] == ["Lunch?"]
    assert idx.unread_count() == 1
    assert [m["subject"] for m in idx.messages(sender="anna")] == ["Lunch?", "Earlier"]


def test_read_first_and_open(agent):
    out = run(agent, "read", argument="first", show=True)
    s = out["speech"]
    assert "Anna Berg" in s and "Lunch?" in s and "free at noon" in s
    assert "old" not in s and "wrote" not in s     # quoted reply stripped
    assert agent.shown == [("mid:m1@x",)]          # exact message shown in Thunderbird


def test_next_previous_and_end(agent):
    run(agent, "read", argument="first")
    assert "Report" in run(agent, "read", argument="next")["speech"]
    assert "Earlier" in run(agent, "read", argument="next")["speech"]
    assert "no more" in run(agent, "read", argument="next")["speech"]
    assert "Report" in run(agent, "read", argument="previous")["speech"]


def test_from_sender_walks_within_sender(agent):
    assert "Lunch?" in run(agent, "read", argument="from:anna")["speech"]
    assert "Earlier" in run(agent, "read", argument="next")["speech"]
    assert run(agent, "read", argument="from:zed")["missing"]


def test_list_unread_then_read_the_first_one(agent):
    s = run(agent, "list", argument="unread")["speech"]
    assert "1 unread email." in s and "Lunch?" in s
    assert "Lunch?" in run(agent, "read", argument="first one")["speech"]


def test_open_current(agent):
    run(agent, "read", argument="second")
    run(agent, "open", argument="current")
    assert agent.shown[-1] == ("mid:m2@x",)


def test_no_profile(monkeypatch):
    monkeypatch.setattr(mail_agent, "find_profile", lambda: None)
    out = asyncio.run(MailAgent(None, None)._execute("read", {"argument": "first"}))
    assert out["missing"] and "Thunderbird" in out["speech"]


def test_find_profile_reads_profiles_ini(tmp_path, monkeypatch):
    root = tmp_path / "tb"
    (root / "abc.default").mkdir(parents=True)
    (root / "abc.default" / "global-messages-db.sqlite").write_bytes(b"")
    (root / "profiles.ini").write_text("[Profile0]\nName=default\nIsRelative=1\nPath=abc.default\nDefault=1\n")
    monkeypatch.delenv("YGGDRASIL_THUNDERBIRD_PROFILE", raising=False)
    monkeypatch.setattr(mail_agent, "_PROFILE_ROOTS", (tmp_path / "nope", root))
    assert find_profile() == root / "abc.default"


def test_cleaners():
    assert _clean_author('"Carol Diaz" <c@x.com> undefined') == "Carol Diaz"
    assert _clean_author("<only@x.com>") == "only@x.com"
    assert _clean_body("see https://example.com/x") == "see a link"
    assert _clean_body("[Go to the dashboard] ( https://x.co/d ) now") == "Go to the dashboard now"
    assert _clean_body("Hi https://a.b/c https://a.b/d bye") == "Hi some links bye"
    assert _clean_body("Thanks!\n-- \nBob\nCEO") == "Thanks!"
    assert _clean_body("Intro. " + "-" * 990 + " More text. ===== End.") == "Intro. More text. End."
