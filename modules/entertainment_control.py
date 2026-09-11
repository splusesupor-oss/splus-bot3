"""کنترل سرگرمی به تفکیک گروه — روشن/خاموش کردن بازی‌های داخلی ربات.

دو دستور متنی داخل گروه:

    «سرگرمی خاموش» → بازی‌های داخلیِ همان گروه غیرفعال می‌شود.
    «سرگرمی فعال»  → دوباره فعال می‌شود.

دسترسی فقط برای مالک اصلی ربات، مالک ثبت‌شدهٔ گروه یا ادمین ثبت‌شدهٔ همان
گروه است (همان ``admin_tools.has_admin_permission`` موجود در پروژه).

وضعیت در فایل مستقل ``entertainment_mode.json`` در پوشهٔ config رانتایم
(برای هر instance جدا) ذخیره می‌شود و **هیچ** داده، سکه، بازی، پاداش یا
تنظیمات موجودی را تغییر نمی‌دهد — فقط یک گارد است.

- پیش‌فرض هر گروه تنظیم‌نشده = فعال.
- خطای خواندن دیسک = فعال (یک خطای دیسک نباید بازی‌ها را برای همیشه ببندد).
- «لیست بازی» عمداً گارد نمی‌شود، چون پیام فعال‌سازی خودش کاربر را به آن
  ارجاع می‌دهد.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file
from modules.atomic_write import write_json

# کتابخانهٔ کلاینت ممکن است در محیط تست نصب نباشد؛ کلاس جایگزین ساده تعریف
# می‌شود تا تست‌ها بدون نصب کتابخانه هم اجرا شوند.
try:
    from splusthon.tl.types import MessageEntityBold
except ImportError:  # pragma: no cover - فقط در محیط‌های بدون splusthon
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


# ---------------------------------------------------------------------------
# دستورها و پیام‌ها — متن‌ها عیناً طبق قرارداد، بدون کم و زیاد کردن کاراکتر
# ---------------------------------------------------------------------------
COMMAND_DISABLE = "سرگرمی خاموش"
COMMAND_ENABLE = "سرگرمی فعال"
COMMANDS = frozenset({COMMAND_DISABLE, COMMAND_ENABLE})

DISABLED_NOTICE = "🎮 بازی های روباه غیر فعال شد"
ENABLED_NOTICE = (
    "🍬 : بازی های روباه فعال شد میتوانید با دستور لیست بازی "
    "از بازی ها استفاده کنید"
)
BLOCKED_NOTICE = (
    "بازی های روباه عموم غیر فعال می‌باشند ؛ برای فعال سازی باید ادمین یا "
    "مالک با دستور سرگرمی فعال بازی هارو فعال کند تا بتوانید از بازی ها "
    "استفاده کنید ☑️"
)
PERMISSION_DENIED = "❌ فقط مالک یا ادمین‌های گروه اجازه تغییر وضعیت سرگرمی را دارند"
PRIVATE_ONLY = "❌ این دستور فقط داخل گروه کار می‌کند."

# ---------------------------------------------------------------------------
# نرمال‌سازی متن
# ---------------------------------------------------------------------------
_NORMALIZE_MAP = {
    "\u200c": " ",       # نیم‌فاصله → فاصله
    "\u200f": "",        # RTL mark حذف
    "\u200e": "",        # LTR mark حذف
    "\u064a": "\u06cc",  # ي عربی → ی فارسی
    "\u0643": "\u06a9",  # ك عربی → ک فارسی
}


def normalize(text):
    """نیم‌فاصله، نویسه‌های جهت‌دهی و حروف عربی را یکسان می‌کند.

    تا «چهار گزینه‌ای» و «چهار گزینه ای» و «حدس ايموجي» همگی درست تشخیص
    داده شوند.
    """
    if not text:
        return ""
    result = str(text)
    for source, target in _NORMALIZE_MAP.items():
        result = result.replace(source, target)
    return " ".join(result.split())


# ---------------------------------------------------------------------------
# فهرست دستورهایی که بازیِ داخلی را اجرا می‌کنند (خودِ دستور شروع، نه پاسخ‌ها)
# ---------------------------------------------------------------------------
GAME_COMMANDS = frozenset({
    normalize(command) for command in (
        # بازی‌های Fox AI (از handlers/fox_games_router.py)
        "بخند یا بباز",
        "بقا",
        "جعبه شانسی",
        "خون آشام",
        "خون‌آشام",
        "معما",
        "حدس جمله",
        "ساخت جمله",
        "مین یاب",
        "بهترین جواب",
        "نبرد",
        "کارگاه",
        "شرکت",
        # بازی‌های داخل هندلر اصلی (handlers/message_handler.py)
        "اسم فامیل",
        "حدس ایموجی",
        "حدس پرچم",
        "تصحیح کلمات",
        "کی بیشتر بلده",
        "دروغ یا حقیقت",
        "چهار گزینه‌ای",
        "جای خالی",
        "چیستان",
        "جک",
        "جرعت",
        "جرات",
        "جرئت",
        "حقیقت",
        "حقیقت بگو",
    )
})


def is_game_command(text):
    """آیا این متن یکی از دستورهای اجرای بازیِ داخلی است؟"""
    return normalize(text) in GAME_COMMANDS


# ---------------------------------------------------------------------------
# ذخیره‌سازی — فایل مستقل entertainment_mode.json با نوشتن اتمیک و کش mtime
# ---------------------------------------------------------------------------
_override_path = None
_cache = None
_cache_mtime = None


def use_file(path):
    """تست‌ها: مسیر فایل را به یک فایل موقت هدایت می‌کند و کش را صفر می‌کند."""
    global _override_path, _cache, _cache_mtime
    _override_path = Path(path)
    _cache = None
    _cache_mtime = None


def reset_cache():
    """کش mtime را صفر می‌کند تا خواندن بعدی حتماً از دیسک باشد (برای تست)."""
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = None


def _file() -> Path:
    if _override_path is not None:
        return _override_path
    return runtime_config_file("entertainment_mode.json")


def _file_mtime():
    try:
        return os.stat(_file()).st_mtime_ns
    except OSError:
        return None


def load():
    """وضعیت همهٔ گروه‌ها را می‌خواند؛ فایل غایب/خراب → دیکشنری خالی."""
    global _cache, _cache_mtime
    mtime = _file_mtime()
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    if mtime is None:
        data = {}
    else:
        try:
            with open(_file(), "r", encoding="utf-8") as stream:
                loaded = json.load(stream)
            data = loaded if isinstance(loaded, dict) else {}
        except Exception:
            # هر خطای دیسک/JSON یعنی «هیچ گروهی خاموش نیست» — بازی‌ها بسته
            # نمی‌شوند و فقط مقدار پیش‌فرض (فعال) برمی‌گردد.
            data = {}
    _cache = data
    _cache_mtime = mtime
    return data


def save(data):
    """نوشتن اتمیک (فایل موقت + rename) تا قطعیِ برق فایل را خراب نکند."""
    global _cache, _cache_mtime
    write_json(_file(), data, indent=2)
    _cache = dict(data)
    _cache_mtime = _file_mtime()


def _key(chat_id):
    """شناسهٔ گروه را پیش از استفاده به‌عنوان کلید نرمال‌سازی می‌کند."""
    return str(normalize_group_id(chat_id))


def is_enabled(chat_id):
    """آیا سرگرمی در این گروه فعال است؟ پیش‌فرض (تنظیم‌نشده/خطا) = فعال."""
    data = load()
    return bool(data.get(_key(chat_id), True))


def set_enabled(chat_id, enabled):
    data = dict(load())
    data[_key(chat_id)] = bool(enabled)
    save(data)


def enable(chat_id):
    set_enabled(chat_id, True)


def disable(chat_id):
    set_enabled(chat_id, False)


def reset(chat_id):
    """کلید گروه را پاک می‌کند تا به پیش‌فرض (فعال) برگردد."""
    data = dict(load())
    key = _key(chat_id)
    if key in data:
        del data[key]
        save(data)


# ---------------------------------------------------------------------------
# Bold واقعی — MessageEntityBold با offset/length بر حسب واحدهای UTF-16
# ---------------------------------------------------------------------------
def u16_length(text):
    """طول رشته بر حسب واحد UTF-16 — همان واحدی که entity لازم دارد."""
    return len(str(text).encode("utf-16-le")) // 2


def full_bold_span(text):
    """(offset, length) برای Bold کردنِ کل متن."""
    return 0, u16_length(text)


def blocked_bold_span(text=None):
    """(offset, length) عبارت «سرگرمی فعال» داخل پیام مسدودی؛ یا None."""
    text = BLOCKED_NOTICE if text is None else text
    index = text.find("سرگرمی فعال")
    if index < 0:
        return None
    return u16_length(text[:index]), u16_length("سرگرمی فعال")


def _bold_entities(text, spans):
    entities = []
    for offset, length in spans:
        entities.append(MessageEntityBold(offset=offset, length=length))
    return entities


async def _reply(event, text, spans):
    """ارسال با formatting_entities؛ اگر کلاینت entity نپذیرد، متن ساده."""
    entities = _bold_entities(text, spans) if spans else None
    try:
        await event.reply(text, formatting_entities=entities)
    except TypeError:
        # کلاینت entity را پشتیبانی نمی‌کند؛ fallback به متن ساده.
        await event.reply(text)


async def send_disabled_notice(event):
    await _reply(event, DISABLED_NOTICE, [full_bold_span(DISABLED_NOTICE)])


async def send_enabled_notice(event):
    await _reply(event, ENABLED_NOTICE, [full_bold_span(ENABLED_NOTICE)])


async def send_blocked_notice(event):
    span = blocked_bold_span()
    await _reply(event, BLOCKED_NOTICE, [span] if span else [])


# ---------------------------------------------------------------------------
# گارد
# ---------------------------------------------------------------------------
def blocks(chat_id, text):
    """گارد خالص، بدون I/O شبکه — برای تست آسان.

    True یعنی این متن یک دستور بازی است و بازی در این گروه خاموش است.
    """
    return is_game_command(text) and not is_enabled(chat_id)


async def guard(event, chat_id, text):
    """True یعنی بازی نباید اجرا شود (و پیام هشدار هم فرستاده شده است)."""
    if not blocks(chat_id, text):
        return False
    await send_blocked_notice(event)
    return True
