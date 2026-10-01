"""Offline tests for the per-group Clipboard workflow.

The tests use only fake events/messages; no Soroush Plus login or network is
required.  They exercise the public module route that the main handler calls.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import clipboard
from modules.spam_detector import SpamDetector


class SentMessage:
    _next_id = 7000

    def __init__(self, text, entities=None):
        SentMessage._next_id += 1
        self.id = SentMessage._next_id
        self.message = text
        self.entities = list(entities or [])


class IncomingMessage:
    _next_id = 100

    def __init__(self, text, entities=None):
        IncomingMessage._next_id += 1
        self.id = IncomingMessage._next_id
        self.message = text
        self.caption = ""
        self.entities = list(entities or [])


class FakeEvent:
    def __init__(self, text, *, reply_to=None, entities=None, is_private=False):
        self.message = IncomingMessage(text, entities)
        self.reply_to = (
            SimpleNamespace(reply_to_msg_id=reply_to)
            if reply_to is not None else None
        )
        self.is_private = is_private
        self.replies = []

    async def reply(self, text, formatting_entities=None, **_kwargs):
        sent = SentMessage(text, formatting_entities)
        self.replies.append(sent)
        return sent


class FakeLogger:
    def __init__(self):
        self.errors = []

    def log_error(self, message):
        self.errors.append(message)


class FakeConfig:
    def __init__(self):
        # Includes a link term deliberately: links must remain valid clipboard
        # content, while the current fuzzy detector still catches spaced سکس.
        self.banned_words = ["سکس", "t.me"]
        self._banned_words_version = 1

    def get(self, key, default=None):
        return True if key == "check_banned_words" else default

    def reload_if_needed(self):
        pass


class FakeBot:
    def __init__(self):
        self.logger = FakeLogger()
        self.detector = SpamDetector(FakeConfig())


def run(awaitable):
    return asyncio.run(awaitable)


def u16(text):
    return len(text.encode("utf-16-le")) // 2


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch, tmp_path):
    """Every test gets a new persisted JSON state file."""
    path = tmp_path / "clipboard.json"
    monkeypatch.setattr(clipboard, "FILE", path)
    yield path


def route(bot, event, chat_id, user_id, text, authorized=True):
    return run(
        clipboard.handle_message(
            bot, event, chat_id, user_id, text, authorized=authorized
        )
    )


def open_guide(bot, chat_id=101, user_id=7):
    event = FakeEvent(clipboard.COMMAND_GUIDE)
    assert route(bot, event, chat_id, user_id, clipboard.COMMAND_GUIDE) is True
    assert len(event.replies) == 1
    return event.replies[0]


def test_guide_is_one_message_with_required_bold_and_quote():
    bot = FakeBot()
    guide = open_guide(bot)

    assert guide.message == clipboard.GUIDE_TEXT
    assert clipboard.pending_guide_id(101) == guide.id
    names = [type(entity).__name__ for entity in guide.entities]
    assert names.count("MessageEntityBold") == 2
    assert names.count("MessageEntityBlockquote") == 1

    quote = next(e for e in guide.entities if type(e).__name__ == "MessageEntityBlockquote")
    assert quote.offset == 0
    assert quote.length == u16(clipboard.TITLE)
    bold_ranges = {(e.offset, e.length) for e in guide.entities if type(e).__name__ == "MessageEntityBold"}
    assert (0, u16(clipboard.TITLE)) in bold_ranges
    assert (u16(clipboard.TITLE + "\n\n"), u16(clipboard.DESCRIPTION)) in bold_ranges


def test_reply_to_exact_guide_saves_and_copy_preserves_mixed_formatting():
    bot = FakeBot()
    guide = open_guide(bot)
    text = "عنوان پررنگ\nلینک سایت\nنقل قول"
    types = clipboard._tl_types
    entities = [
        types.MessageEntityBold(offset=0, length=u16("عنوان پررنگ")),
        types.MessageEntityTextUrl(
            offset=u16("عنوان پررنگ\n"),
            length=u16("لینک سایت"),
            url="https://example.com",
        ),
        types.MessageEntityBlockquote(
            offset=u16("عنوان پررنگ\nلینک سایت\n"),
            length=u16("نقل قول"),
        ),
    ]
    save = FakeEvent(text, reply_to=guide.id, entities=entities)
    assert route(bot, save, 101, 7, text) is True
    assert save.replies[-1].message == clipboard.SAVED_MESSAGE

    # The stored JSON is restart-safe: get_clipboard rereads it rather than
    # relying on any in-memory pending or formatting cache.
    stored = clipboard.get_clipboard(101)
    assert stored["text"] == text
    assert len(stored["entities"]) == 3

    send = FakeEvent(clipboard.COMMAND_SEND)
    assert route(bot, send, 101, 7, clipboard.COMMAND_SEND) is True
    output = send.replies[-1]
    assert output.message == text
    assert [type(e).__name__ for e in output.entities] == [
        "MessageEntityBold", "MessageEntityTextUrl", "MessageEntityBlockquote"
    ]
    assert output.entities[1].url == "https://example.com"
    assert output.entities[2].offset == u16("عنوان پررنگ\nلینک سایت\n")


def test_plain_link_is_allowed_and_is_sent_unchanged():
    bot = FakeBot()
    guide = open_guide(bot)
    text = "لینک اطلاعیه: https://t.me/example_channel"
    save = FakeEvent(text, reply_to=guide.id)
    assert route(bot, save, 101, 7, text) is True
    assert save.replies[-1].message == clipboard.SAVED_MESSAGE

    send = FakeEvent(clipboard.COMMAND_SEND)
    assert route(bot, send, 101, 7, clipboard.COMMAND_SEND) is True
    assert send.replies[-1].message == text


def test_new_reply_replaces_only_its_own_group_clipboard_and_persists():
    bot = FakeBot()
    first_guide = open_guide(bot, chat_id=101)
    first = FakeEvent("نسخه اول", reply_to=first_guide.id)
    assert route(bot, first, 101, 7, "نسخه اول") is True

    second_guide = open_guide(bot, chat_id=101)
    second = FakeEvent("نسخه دوم\nخط دوم", reply_to=second_guide.id)
    assert route(bot, second, 101, 8, second.message.message) is True
    assert clipboard.get_clipboard(101)["text"] == "نسخه دوم\nخط دوم"

    # A separate group cannot read or replace group A's value.
    missing = FakeEvent(clipboard.COMMAND_SEND)
    assert route(bot, missing, 202, 7, clipboard.COMMAND_SEND) is True
    assert missing.replies[-1].message == clipboard.MISSING_MESSAGE
    assert clipboard.get_clipboard(202) is None

    # A new module-level read still obtains the durable JSON record.
    assert clipboard.get_clipboard(101)["text"] == "نسخه دوم\nخط دوم"


def test_plain_message_or_wrong_reply_never_stores_clipboard():
    bot = FakeBot()
    guide = open_guide(bot)

    plain = FakeEvent("نباید ذخیره شود")
    assert route(bot, plain, 101, 7, plain.message.message) is False
    assert clipboard.get_clipboard(101) is None

    wrong_reply = FakeEvent("باز هم نباید ذخیره شود", reply_to=guide.id + 1)
    assert route(bot, wrong_reply, 101, 7, wrong_reply.message.message) is False
    assert clipboard.get_clipboard(101) is None


def test_unauthorized_user_cannot_open_save_or_send():
    bot = FakeBot()
    command = FakeEvent(clipboard.COMMAND_GUIDE)
    assert route(bot, command, 101, 99, clipboard.COMMAND_GUIDE, authorized=False) is True
    assert command.replies[-1].message == clipboard.PERMISSION_DENIED
    assert clipboard.pending_guide_id(101) is None

    guide = open_guide(bot)
    attempted_save = FakeEvent("متن غیرمجاز", reply_to=guide.id)
    assert route(bot, attempted_save, 101, 99, attempted_save.message.message, authorized=False) is True
    assert attempted_save.replies[-1].message == clipboard.PERMISSION_DENIED
    assert clipboard.get_clipboard(101) is None


def test_prohibited_obfuscated_content_and_too_long_text_keep_previous_value():
    bot = FakeBot()
    guide = open_guide(bot)
    initial = FakeEvent("متن سالم", reply_to=guide.id)
    assert route(bot, initial, 101, 7, initial.message.message) is True

    # The configured SpamDetector's fuzzy matcher catches spaced characters,
    # while name_filter/photo policy cover profanity and explicit content.
    blocked_guide = open_guide(bot)
    blocked = FakeEvent("س ک س", reply_to=blocked_guide.id)
    assert route(bot, blocked, 101, 7, blocked.message.message) is True
    assert blocked.replies[-1].message == clipboard.BLOCKED_MESSAGE
    assert clipboard.get_clipboard(101)["text"] == "متن سالم"

    long_guide = open_guide(bot)
    too_long_text = "ا" * (clipboard.MAX_MESSAGE_UTF16 + 1)
    too_long = FakeEvent(too_long_text, reply_to=long_guide.id)
    assert route(bot, too_long, 101, 7, too_long_text) is True
    assert too_long.replies[-1].message == clipboard.TOO_LONG_MESSAGE
    assert clipboard.get_clipboard(101)["text"] == "متن سالم"


def test_empty_reply_and_private_command_are_safe():
    bot = FakeBot()
    guide = open_guide(bot)
    empty = FakeEvent("   \n", reply_to=guide.id)
    assert route(bot, empty, 101, 7, empty.message.message) is True
    assert empty.replies[-1].message == clipboard.EMPTY_MESSAGE
    assert clipboard.get_clipboard(101) is None

    private = FakeEvent(clipboard.COMMAND_SEND, is_private=True)
    assert route(bot, private, 999, 7, clipboard.COMMAND_SEND, authorized=False) is True
    assert private.replies[-1].message == clipboard.PRIVATE_ONLY
