"""Persistent, per-group Clipboard for the Fox bot.

The feature deliberately has a narrow state machine:

``کپی بورد`` → the bot sends one guide → an authorized manager replies to
*that exact guide message* → the reply is saved.  A normal message is never
captured merely because a clipboard flow was started.

Mutable state uses :func:`runtime_config_file`, so Termux/private runtime
folders and ``BOT_INSTANCE`` isolation work exactly like the rest of the bot.
The JSON write is atomic, and the saved formatting entities are serialized so
bold, blockquotes, text links and other SPlusthon-supported text entities
survive a restart.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from economy import name_filter
from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file

try:  # The production client provides the real SPlusthon TL classes.
    from splusthon.tl import types as _tl_types
except ImportError:  # Keep offline unit tests independent from a live client.
    class _FallbackEntity:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _FallbackTypes:
        _classes: dict[str, type] = {}

        def __getattr__(self, name):
            if not name.startswith("MessageEntity"):
                raise AttributeError(name)
            cls = self._classes.get(name)
            if cls is None:
                cls = type(name, (_FallbackEntity,), {})
                self._classes[name] = cls
            return cls

    _tl_types = _FallbackTypes()

try:
    # This is the current bot-wide policy for sexually explicit search requests.
    # It normalizes Persian/Arabic variants and has Persian + English patterns.
    from modules.photo_download import is_blocked as _photo_content_blocked
except Exception:  # Never make clipboard unavailable if an optional dependency fails.
    _photo_content_blocked = lambda _value: False


FILE = runtime_config_file("clipboard.json")
NAMESPACE_VERSION = 1
MAX_MESSAGE_UTF16 = 4096

COMMAND_GUIDE = "کپی بورد"
COMMAND_SEND = "کپی"
COMMANDS = frozenset({COMMAND_GUIDE, COMMAND_SEND})

TITLE = "🔖راهنما و توضیحات کپی بورد"
DESCRIPTION = (
    "قابلیت کپی بورد به این صورت می‌باشد که میتوانید یک متن به هم به صورت ساده و هم ب صورت bold شده و یا نقل قول شده به ربات بدهید و بعدا با دستور کپی بورد اون متن رو ارسال میکنه اما چه استفاده ای می‌تونه داشته باشه مثلا در مواردی مثل وقتی که گروه میبندید میخایین بعد از قفل شدن لینکی رو برای سین زدن یا برای دیده شدن فواورد کنید ربات مستقیم نشان میده یا موقع چت گروهی میتوانید بدون نیاز به فواورد از کانالی یا پیام شخصی لینک کانال گروه دوم یا پیوی خصوصی و یا تکست و اعلان های مهم گروه رو با هر شکل و توضیحی به ربات بدهید و با همون دستور ربات سریع نمایش خواهد داد"
)
FOOTER = "📌روی همین پیام ریپلای کنید و متن یا تکست و لینک خودتون رو بفرستید"
GUIDE_TEXT = f"{TITLE}\n\n{DESCRIPTION}\n\n{FOOTER}"

PERMISSION_DENIED = "❌ فقط مالک ثبت‌شده یا ادمین ربات اجازه استفاده از کپی بورد را دارد."
PRIVATE_ONLY = "❌ قابلیت کپی بورد فقط داخل گروه فعال است."
EMPTY_MESSAGE = "❌ متن یا تکست معتبر برای ذخیره ارسال نشده است."
TOO_LONG_MESSAGE = "❌ متن کپی بورد از محدودیت پیام سروش پلاس طولانی‌تر است."
BLOCKED_MESSAGE = "❌ این متن به دلیل داشتن محتوای غیرمجاز قابل ذخیره در کپی بورد نیست."
SAVED_MESSAGE = "✅ متن کپی بورد با موفقیت ذخیره شد."
MISSING_MESSAGE = "❌ هنوز متنی برای کپی ذخیره نشده است."
SEND_FAILED_MESSAGE = "❌ ارسال متن کپی بورد با خطا مواجه شد."


def _u16_len(value: str) -> int:
    return len(str(value or "").encode("utf-16-le")) // 2


def _key(chat_id) -> str:
    return normalize_group_id(chat_id)


def _load() -> dict:
    try:
        raw = json.loads(Path(FILE).read_text(encoding="utf-8")) if Path(FILE).exists() else {}
    except (OSError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save(data: dict) -> None:
    Path(FILE).parent.mkdir(parents=True, exist_ok=True)
    write_json(FILE, data, indent=2)


def _record_for(data: dict, chat_id, *, create: bool = False) -> dict | None:
    key = _key(chat_id)
    record = data.get(key)
    if isinstance(record, dict):
        return record
    if not create:
        return None
    record = {"version": NAMESPACE_VERSION}
    data[key] = record
    return record


def pending_guide_id(chat_id):
    record = _record_for(_load(), chat_id)
    if not record:
        return None
    try:
        value = record.get("guide_message_id")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def set_pending_guide(chat_id, message_id) -> bool:
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        return False
    if message_id <= 0:
        return False
    data = _load()
    record = _record_for(data, chat_id, create=True)
    record["version"] = NAMESPACE_VERSION
    record["guide_message_id"] = message_id
    record["guide_created_at"] = int(time.time())
    _save(data)
    return True


def _reply_message_id(event):
    reply_to = getattr(event, "reply_to", None)
    if reply_to is None:
        return None
    for field in ("reply_to_msg_id", "reply_to_top_id", "msg_id"):
        value = getattr(reply_to, field, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def is_reply_to_pending_guide(chat_id, event) -> bool:
    reply_id = _reply_message_id(event)
    guide_id = pending_guide_id(chat_id)
    return bool(reply_id and guide_id and reply_id == guide_id)


def _json_value(value: Any):
    """Return a JSON-safe TL field value, or ``None`` for unsupported values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (tuple, list)):
        output = []
        for item in value:
            parsed = _json_value(item)
            if parsed is _UNSUPPORTED:
                return _UNSUPPORTED
            output.append(parsed)
        return output
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            parsed = _json_value(item)
            if parsed is _UNSUPPORTED:
                return _UNSUPPORTED
            output[str(key)] = parsed
        return output
    return _UNSUPPORTED


_UNSUPPORTED = object()


def _entity_fields(entity) -> dict | None:
    """Extract serializable constructor fields from a SPlusthon TL entity."""
    values: dict[str, Any] = {}
    to_dict = getattr(entity, "to_dict", None)
    if callable(to_dict):
        try:
            exported = to_dict()
            if isinstance(exported, dict):
                values.update({key: value for key, value in exported.items() if key != "_"})
        except Exception:
            values = {}
    if not values:
        raw = getattr(entity, "__dict__", None)
        if isinstance(raw, dict):
            values.update(raw)
    # Generated TL classes commonly use __slots__ instead of __dict__.
    for cls in type(entity).__mro__:
        for slot in getattr(cls, "__slots__", ()) or ():
            if slot.startswith("_") or slot in values:
                continue
            try:
                values[slot] = getattr(entity, slot)
            except (AttributeError, TypeError):
                continue

    clean: dict[str, Any] = {}
    for key, value in values.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        parsed = _json_value(value)
        if parsed is _UNSUPPORTED:
            # Preserve the message safely even when one optional rich entity
            # has a complex peer/document object that cannot be rebuilt.
            return None
        clean[key] = parsed
    return clean


def serialize_entities(entities, text: str) -> list[dict]:
    """Serialize valid text entities while retaining offsets and link targets."""
    max_length = _u16_len(text)
    output: list[dict] = []
    for entity in list(entities or []):
        type_name = type(entity).__name__
        if not type_name.startswith("MessageEntity"):
            continue
        try:
            offset = int(getattr(entity, "offset"))
            length = int(getattr(entity, "length"))
        except (AttributeError, TypeError, ValueError):
            continue
        if offset < 0 or length < 0 or offset + length > max_length:
            continue
        fields = _entity_fields(entity)
        if fields is None:
            continue
        # Offset/length always come from the validated runtime entity.
        fields["offset"] = offset
        fields["length"] = length
        output.append({"type": type_name, "fields": fields})
    return output


def deserialize_entities(records, text: str) -> list:
    """Rebuild supported SPlusthon entities for ``formatting_entities=``."""
    max_length = _u16_len(text)
    output = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        type_name = record.get("type")
        fields = record.get("fields")
        if not isinstance(type_name, str) or not type_name.startswith("MessageEntity"):
            continue
        if not isinstance(fields, dict):
            continue
        try:
            offset = int(fields.get("offset"))
            length = int(fields.get("length"))
        except (TypeError, ValueError):
            continue
        if offset < 0 or length < 0 or offset + length > max_length:
            continue
        try:
            cls = getattr(_tl_types, type_name)
            output.append(cls(**dict(fields)))
        except Exception:
            # An unknown/new API entity must not prevent the saved text from
            # being sent. The remaining compatible entities stay intact.
            continue
    return output


def _clipboard_record(record: dict | None):
    if not isinstance(record, dict):
        return None
    clipboard = record.get("clipboard")
    if not isinstance(clipboard, dict):
        return None
    text = clipboard.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if _u16_len(text) > MAX_MESSAGE_UTF16:
        return None
    entities = clipboard.get("entities")
    return {
        "text": text,
        "entities": list(entities) if isinstance(entities, list) else [],
        "updated_by": str(clipboard.get("updated_by", "")),
    }


def get_clipboard(chat_id):
    return _clipboard_record(_record_for(_load(), chat_id))


def save_clipboard(chat_id, user_id, text: str, entities) -> bool:
    """Atomically replace this group's clipboard after caller-side validation."""
    if not isinstance(text, str) or not text.strip() or _u16_len(text) > MAX_MESSAGE_UTF16:
        return False
    data = _load()
    record = _record_for(data, chat_id, create=True)
    record["version"] = NAMESPACE_VERSION
    record["clipboard"] = {
        "text": text,
        "entities": serialize_entities(entities, text),
        "updated_by": str(user_id),
        "updated_at": int(time.time()),
    }
    # A valid reply consumes the one pending guide; any later reply to it is
    # just an ordinary chat message and cannot overwrite the clipboard.
    record.pop("guide_message_id", None)
    record.pop("guide_created_at", None)
    _save(data)
    return True


def _message_text(event) -> str:
    message = getattr(event, "message", None)
    if message is None:
        return ""
    return getattr(message, "message", None) or getattr(message, "caption", None) or ""


def _message_entities(event):
    message = getattr(event, "message", None)
    return list(getattr(message, "entities", None) or []) if message is not None else []


def prohibited_content(text: str, detector=None, chat_id=None) -> bool:
    """Use the bot's existing robust content filters before a clipboard write.

    ``economy.name_filter`` supplies the main fuzzy profanity detector
    (invisible characters, stretched characters, leetspeak and Persian/Arabic
    variants).  ``photo_download.is_blocked`` is the existing Persian/English
    sexually-explicit-content policy.  Finally, matching content terms already
    configured in the anti-spam engine are checked through that engine's fuzzy
    patterns, without treating ordinary URLs/links as prohibited clipboard
    content.
    """
    if name_filter.classify(text) == name_filter.BANNED:
        return True
    try:
        if _photo_content_blocked(text):
            return True
    except Exception:
        pass

    # Reuse the existing fuzzy banned-word engine for configured *content*
    # words only.  The configured list also contains link/advertising entries;
    # those must stay allowed because clipboard explicitly supports links.
    if detector is None:
        return False
    try:
        detector._refresh_banned_word_patterns()
        haystack = detector._fold_banned_letters(text)
        for word, pattern in detector._banned_word_patterns:
            try:
                is_content_word = (
                    name_filter.classify(word) == name_filter.BANNED
                    or _photo_content_blocked(word)
                )
            except Exception:
                is_content_word = name_filter.classify(word) == name_filter.BANNED
            if is_content_word and pattern.search(haystack):
                return True
    except Exception:
        # Safety checks never break the command path because of a stale/custom
        # detector. The two independent filters above have already run.
        pass
    return False


def guide_entities() -> list:
    title_length = _u16_len(TITLE)
    description_offset = _u16_len(f"{TITLE}\n\n")
    return [
        _tl_types.MessageEntityBlockquote(offset=0, length=title_length),
        _tl_types.MessageEntityBold(offset=0, length=title_length),
        _tl_types.MessageEntityBold(
            offset=description_offset,
            length=_u16_len(DESCRIPTION),
        ),
    ]


async def handle_message(
    bot,
    event,
    chat_id,
    user_id,
    clean_text: str,
    *,
    authorized: bool,
) -> bool:
    """Handle clipboard commands/replies and return whether the event was used.

    The caller supplies the existing project authorization decision; this
    module intentionally does not introduce a second admin system.
    """
    is_private = bool(getattr(event, "is_private", False))
    command = str(clean_text or "").strip()
    pending_reply = (not is_private) and is_reply_to_pending_guide(chat_id, event)

    if command not in COMMANDS and not pending_reply:
        return False

    if is_private:
        if command in COMMANDS:
            await event.reply(PRIVATE_ONLY)
            return True
        return False

    if not authorized:
        await event.reply(PERMISSION_DENIED)
        return True

    if command == COMMAND_GUIDE:
        sent = await event.reply(GUIDE_TEXT, formatting_entities=guide_entities())
        message_id = getattr(sent, "id", None)
        if message_id is not None:
            set_pending_guide(chat_id, message_id)
        else:
            # Do not accept a reply unless the exact guide ID is available.
            logger = getattr(bot, "logger", None)
            if logger is not None:
                try:
                    logger.log_error(
                        f"CLIPBOARD GUIDE ID MISSING chat_id={chat_id} user_id={user_id}"
                    )
                except Exception:
                    pass
        return True

    if command == COMMAND_SEND:
        saved = get_clipboard(chat_id)
        if not saved:
            await event.reply(MISSING_MESSAGE)
            return True
        try:
            await event.reply(
                saved["text"],
                formatting_entities=deserialize_entities(
                    saved.get("entities"), saved["text"]
                ) or None,
            )
        except Exception as error:
            logger = getattr(bot, "logger", None)
            if logger is not None:
                try:
                    logger.log_error(
                        f"CLIPBOARD SEND FAILED chat_id={chat_id} error={error!r}"
                    )
                except Exception:
                    pass
            await event.reply(SEND_FAILED_MESSAGE)
        return True

    # The only remaining consumed path is a reply to the exact pending guide.
    raw_text = _message_text(event)
    if not raw_text.strip():
        await event.reply(EMPTY_MESSAGE)
        return True
    if _u16_len(raw_text) > MAX_MESSAGE_UTF16:
        await event.reply(TOO_LONG_MESSAGE)
        return True
    if prohibited_content(raw_text, getattr(bot, "detector", None), chat_id):
        await event.reply(BLOCKED_MESSAGE)
        return True
    if not save_clipboard(chat_id, user_id, raw_text, _message_entities(event)):
        await event.reply(EMPTY_MESSAGE)
        return True
    await event.reply(SAVED_MESSAGE)
    return True
