"""تشخیص مستقل نام‌های تبلیغاتی؛ جدا از فیلتر متن گروه.

این ماژول همان فیلتر قدیمی نام تبلیغاتی است، با یک لایهٔ نرمال‌سازی قوی‌تر
برای نویسه‌های هم‌ارز فارسی/عربی، نیم‌فاصله، کشیده، حروف تکراری و جداکننده‌ها.
الگوهای کوتاه و عمومی مرزبندی می‌شوند تا بخشی از یک نام/واژهٔ سالم باعث
false-positive نشود؛ دو عبارت پرخطر «سکس» و «بیوگرافی» عمداً در حالت‌های
چسبیده و شکسته نیز تشخیص داده می‌شوند.
"""
import json
import os
import re
import unicodedata
from pathlib import Path

from modules.atomic_write import write_json
from modules.group_id import normalize_group_id
from modules.runtime_paths import runtime_config_file
from modules.user_display import format_user

try:
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:  # pragma: no cover - offline tests without SPlusthon
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length


# الگوهای قدیمی حفظ شده‌اند تا رفتارهای درست قبلی از بین نروند. ورودی این
# regexها ابتدا با _norm یکسان‌سازی و سپس با _collapse از تکرار خالی می‌شود.
_LEGACY_TERMS = (
    r"بیو\s*چک", r"چک\s*بیو", r"بیوگرافی\s*چک", r"بیومو\s*(?:چک|ببینید|ببین)",
    r"بیو.*(?:فیلم|لینک|چک|ببین)",
    r"حال\s*پی",
    r"تمام\s*سانسور",
    r"حال\s*می(?:د|ذ)م",
    r"فیلم\s*پی",
    r"🔞",
    r"پی\s*وی",
    r"پیوی",
    r"\bpv\b",
    r"خاله",
    r"صیغه",
    r"رایگان",
    r"سکسی",
    r"پورن",
    r"نود",
    r"فیلتر\s*شکن",
    r"فیلترشکن",
    r"\bvpn\b",
    r"شارژ\s*رایگان",
    r"پکیج",
    r"ارز\s*دیجیتال",
    r"تتر",
    r"پهلوی",
    r"شاهزاده",
    r"شاه\s*زاده",
    r"پرچم\s*(?:آمریکا|امریکا)",
    r"دلباخته\s*پهلوی",
    r"رضا\s*شاه",
    r"رضاشاه",
    r"محمدرضا\s*شاه",
    r"جان\s*فدای\s*میهن",
    r"جانفدای\s*میهن",
    r"فرزند\s*ایران",
)
_LEGACY_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in _LEGACY_TERMS)

_REQUIRED_EMOJIS = frozenset(("💦", "🌈", "👄", "💋", "🤤", "😰", "🥵", "🍑"))

# (عبارت، آیا درون واژهٔ چسبیده هم مجاز است؟)
# برای واژه‌های عمومی مرز Unicode لازم است؛ مثلاً «گروهان» نباید صرف وجود
# «گروه» مسدود شود. «سکس» و «بیوگرافی» طبق نیاز محصول عمداً سخت‌گیرانه‌اند.
_ROBUST_TERMS = (
    ("سکس", True),
    ("بیوگرافی", True),
    ("فیلم", False),
    ("زوری", False),
    ("کانال", False),
    ("گروه", False),
    ("یکی بیاد", False),
    ("خانوم", False),
    ("پسر", False),
    ("دختر", False),
)

# نویسه‌های هم‌ارز/بسیار مشابهی که در ورودی فارسی رایج‌اند. NFKC شکل‌های
# presentation (مانند ﯽ/ﮐ) را پیش از این جدول به شکل پایه برمی‌گرداند.
_CHAR_EQUIVALENTS = str.maketrans({
    "ي": "ی", "ى": "ی", "ے": "ی", "ۓ": "ی",
    "ك": "ک", "ڪ": "ک", "ګ": "ک", "ڬ": "ک", "ڭ": "ک", "ک": "ک",
    "ۀ": "ه", "ة": "ه", "ہ": "ه", "ھ": "ه",
    "ؤ": "و",
    "أ": "ا", "إ": "ا", "ٱ": "ا", "آ": "ا",
})


def _norm(value, *, retain_symbols=False):
    """فرم امن مقایسه با حفظ حروف/عدد و emojiهای لازم.

    مسیر قدیمی تشخیص تبلیغ، جداکننده‌هایی مانند ``+`` و ``|`` را فاصله
    می‌داند. فیلترهای سفارشی فقط ``retain_symbols`` را فعال می‌کنند تا emoji
    و نمادهایی مانند 🍆 نیز بتوانند خودِ عبارت فیلتر باشند. حرکات، variation
    selector، کنترل‌های bidi، zero-width و کشیده حذف می‌شوند تا نتوانند داخل
    یک حرف پنهان بمانند.
    """
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    value = value.translate(_CHAR_EQUIVALENTS)

    out = []
    for char in value:
        if char in _REQUIRED_EMOJIS or char == "🔞":
            out.append(char)
            continue
        if char == "\u0640":  # Arabic tatweel / کشیده
            continue
        category = unicodedata.category(char)
        if category in {"Mn", "Me", "Cf"}:  # حرکت و نویسهٔ نامرئی
            continue
        if category[0] in {"L", "N"} or (retain_symbols and category[0] == "S"):
            out.append(char)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def _collapse(value):
    """جمع کردن حروف تکراری: «بییییوگرافی» → «بیوگرافی»."""
    return re.sub(r"(.)\1+", r"\1", value)


def _literal_pattern(term, embedded=False, *, retain_symbols=False):
    """regex یک عبارت با فاصلهٔ اختیاری و تکرار اختیاری هر حرف."""
    compact = _norm(term, retain_symbols=retain_symbols).replace(" ", "")
    # هم «سسس» و هم «س س س» تکرار همان حرف شمرده می‌شوند. جداکنندهٔ
    # اختیاری میان گروه‌ها شکل‌های «س.کـس» و «ب ی و گ ر ا ف ی» را می‌گیرد.
    letter_groups = [
        rf"(?:{re.escape(char)}+(?:\s+{re.escape(char)}+)*)"
        for char in compact
    ]
    body = r"\s*".join(letter_groups)
    if embedded:
        return re.compile(body, re.IGNORECASE)
    # Python's \w is Unicode-aware, so «گروهان» و «فیلمبردار» match نمی‌شوند،
    # اما «د خ ت ر»، «دختر-پیوی» و مرز ایموجی درست تشخیص داده می‌شوند.
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


_ROBUST_PATTERNS = tuple(
    (term, _literal_pattern(term, embedded))
    for term, embedded in _ROBUST_TERMS
)


# ---------------------------------------------------------------------------
# فیلترهای نامِ تعریف‌شده توسط ادمین — همان detector و normalizer بالا
# ---------------------------------------------------------------------------
# فایل runtime در هر BOT_INSTANCE جداست و با شناسهٔ canonical گروه کلید
# می‌خورد؛ بنابراین هیچ فیلتر اسمی میان گروه‌ها یا instanceها نشت نمی‌کند.
_FILTER_FILE = runtime_config_file("ad_name_filters.json")
_FILTER_OVERRIDE = None
_FILTER_CACHE = None
_FILTER_CACHE_MTIME = None
MAX_GROUP_FILTERS = 200
MAX_FILTER_CHARS = 80

ADD_PREFIX = "فیلتر اسم "
REMOVE_PREFIX = "حذف فیلتر اسم "
CANCEL_PREFIX = "لغو اسم "
LIST_COMMAND = "لیست فیلتر اسم"
COMMANDS = frozenset({LIST_COMMAND})
COMMAND_PREFIXES = (ADD_PREFIX, REMOVE_PREFIX, CANCEL_PREFIX)

PERMISSION_DENIED = "❌ فقط مالک ثبت‌شده یا ادمین ثبت‌شده اجازه مدیریت فیلتر اسم را دارد."
PRIVATE_ONLY = "❌ این دستور فقط داخل گروه کار می‌کند."


def use_filter_file(path):
    """مسیر موقتِ قابل ایزوله برای تست‌ها؛ ``None`` مسیر runtime را برمی‌گرداند."""
    global _FILTER_OVERRIDE, _FILTER_CACHE, _FILTER_CACHE_MTIME
    _FILTER_OVERRIDE = Path(path) if path is not None else None
    _FILTER_CACHE = None
    _FILTER_CACHE_MTIME = None


def reset_filter_cache():
    global _FILTER_CACHE, _FILTER_CACHE_MTIME
    _FILTER_CACHE = None
    _FILTER_CACHE_MTIME = None


def _filter_file() -> Path:
    return _FILTER_OVERRIDE if _FILTER_OVERRIDE is not None else _FILTER_FILE


def _filter_mtime():
    try:
        return os.stat(_filter_file()).st_mtime_ns
    except OSError:
        return None


def _filter_key(chat_id) -> str:
    return str(normalize_group_id(chat_id))


def _load_filters() -> dict:
    global _FILTER_CACHE, _FILTER_CACHE_MTIME
    mtime = _filter_mtime()
    if _FILTER_CACHE is not None and _FILTER_CACHE_MTIME == mtime:
        return _FILTER_CACHE
    if mtime is None:
        data = {}
    else:
        try:
            decoded = json.loads(_filter_file().read_text(encoding="utf-8"))
            data = decoded if isinstance(decoded, dict) else {}
        except (OSError, ValueError, TypeError):
            data = {}
    _FILTER_CACHE = data
    _FILTER_CACHE_MTIME = mtime
    return data


def _save_filters(data: dict) -> None:
    global _FILTER_CACHE, _FILTER_CACHE_MTIME
    path = _filter_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data, indent=2)
    _FILTER_CACHE = data
    _FILTER_CACHE_MTIME = _filter_mtime()


def _clean_filter_value(value):
    display = str(value or "").strip()
    normalized = _norm(display, retain_symbols=True)
    if not display or not normalized:
        return None, None
    if len(display) > MAX_FILTER_CHARS or len(normalized) > MAX_FILTER_CHARS:
        return None, None
    return display, normalized


def list_group_filters(chat_id) -> list[str]:
    """فهرست نمایش‌دادنی فیلترهای همین گروه، بدون افشای گروه‌های دیگر."""
    rows = _load_filters().get(_filter_key(chat_id), [])
    result = []
    seen = set()
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        display, normalized = _clean_filter_value(row.get("value"))
        if display is None or normalized in seen:
            continue
        seen.add(normalized)
        result.append(display)
    return result


def add_group_filter(chat_id, value):
    """یک عبارت را با normalizer همین detector به گروه اضافه می‌کند.

    خروجی: ``(status, display)`` که status یکی از ``added``, ``exists``,
    ``invalid`` یا ``limit`` است.
    """
    display, normalized = _clean_filter_value(value)
    if display is None:
        return "invalid", None
    data = _load_filters()
    key = _filter_key(chat_id)
    rows = data.get(key, [])
    rows = list(rows) if isinstance(rows, list) else []
    current = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        old_display, old_normalized = _clean_filter_value(row.get("value"))
        if old_display is None or old_normalized in seen:
            continue
        seen.add(old_normalized)
        current.append({"value": old_display, "normalized": old_normalized})
    if normalized in seen:
        return "exists", next(
            row["value"] for row in current if row["normalized"] == normalized
        )
    if len(current) >= MAX_GROUP_FILTERS:
        return "limit", None
    current.append({"value": display, "normalized": normalized})
    data[key] = current
    _save_filters(data)
    return "added", display


def remove_group_filter(chat_id, value):
    """فقط همان فیلتر نرمال‌شدهٔ گروه را حذف می‌کند."""
    _display, normalized = _clean_filter_value(value)
    if normalized is None:
        return "invalid", None
    data = _load_filters()
    key = _filter_key(chat_id)
    rows = data.get(key, [])
    if not isinstance(rows, list):
        return "missing", None
    kept = []
    removed = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        display, row_normalized = _clean_filter_value(row.get("value"))
        if display is None:
            continue
        if removed is None and row_normalized == normalized:
            removed = display
            continue
        kept.append({"value": display, "normalized": row_normalized})
    if removed is None:
        return "missing", None
    if kept:
        data[key] = kept
    else:
        data.pop(key, None)
    _save_filters(data)
    return "removed", removed


def _is_emoji_filter(normalized: str) -> bool:
    """Emoji باید در هر جای نام match شود، نه فقط بین دو حرف."""
    return any(unicodedata.category(char).startswith("S") for char in normalized)


def custom_filter_reason(chat_id, user) -> str | None:
    """فقط نام نمایشی را با فیلترهای همان گروه بررسی می‌کند."""
    if chat_id is None or user is None:
        return None
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    display_name = _norm(f"{first} {last}".strip(), retain_symbols=True)
    if not display_name:
        return None
    for value in list_group_filters(chat_id):
        _display, normalized = _clean_filter_value(value)
        if not normalized:
            continue
        pattern = _literal_pattern(
            normalized,
            embedded=_is_emoji_filter(normalized),
            retain_symbols=True,
        )
        for candidate in (display_name, _collapse(display_name)):
            if pattern.search(candidate):
                return f"فیلتر اسم ({value})"
    return None


def parse_filter_command(text):
    """عملیات مدیریت فیلتر اسم را برمی‌گرداند، یا ``None``."""
    value = str(text or "").strip()
    if value == LIST_COMMAND:
        return "list", None
    for action, prefix in (
        ("add", ADD_PREFIX),
        ("remove", REMOVE_PREFIX),
        ("remove", CANCEL_PREFIX),
    ):
        if value == prefix.strip():
            return action, ""
        if value.startswith(prefix):
            return action, value[len(prefix):].strip()
    return None


def _u16_len(value: str) -> int:
    return len(str(value or "").encode("utf-16-le")) // 2


def _success_entities(text: str):
    length = _u16_len(text)
    return [
        MessageEntityBold(offset=0, length=length),
        MessageEntityBlockquote(offset=0, length=length),
    ]


async def handle_filter_command(event, chat_id, text, *, authorized: bool,
                                is_private: bool = False) -> bool:
    """اجرای دستورهای مدیریت؛ permission از سیستم موجود handler می‌آید."""
    parsed = parse_filter_command(text)
    if parsed is None:
        return False
    action, value = parsed
    if is_private:
        await event.reply(PRIVATE_ONLY)
        return True
    if not authorized:
        await event.reply(PERMISSION_DENIED)
        return True
    if action == "list":
        filters = list_group_filters(chat_id)
        if filters:
            await event.reply("📋 فیلترهای اسم همین گروه:\n\n" + "\n".join(filters))
        else:
            await event.reply("📋 هنوز فیلتری برای نام‌های این گروه ثبت نشده است.")
        return True
    if not value:
        await event.reply(
            "❌ بعد از دستور، نام یا عبارت موردنظر را بنویسید."
        )
        return True
    if action == "add":
        status, display = add_group_filter(chat_id, value)
        if status == "added":
            notice = f"نام : {display} فیلتر شد"
            await event.reply(notice, formatting_entities=_success_entities(notice))
        elif status == "exists":
            await event.reply("⚠️ این نام قبلاً برای همین گروه فیلتر شده است.")
        elif status == "limit":
            await event.reply("❌ ظرفیت فیلترهای نام این گروه پر شده است.")
        else:
            await event.reply("❌ نام یا عبارت فیلتر نامعتبر است.")
        return True

    status, display = remove_group_filter(chat_id, value)
    if status == "removed":
        # متن موفقیت باید همان عبارتی را نشان دهد که مدیر در دستور نوشت.
        notice = f"نام : {value} از فیلتر خارج شد"
        await event.reply(notice, formatting_entities=_success_entities(notice))
    elif status == "missing":
        await event.reply("❌ این نام در فیلترهای همین گروه نیست.")
    else:
        await event.reply("❌ نام یا عبارت فیلتر نامعتبر است.")
    return True


def display_name(user):
    return format_user(user)


def reason(user, chat_id=None):
    """دلیل تشخیص نام تبلیغاتی/فیلتر گروه را برمی‌گرداند.

    قواعد سراسری قدیمی برای username و نام نمایشی برقرار می‌مانند. فیلترهای
    تعریف‌شده توسط ادمین عمداً فقط روی *نام نمایشی* و فقط در ``chat_id``
    خودشان اعمال می‌شوند؛ بنابراین یک نام عادی در گروه دیگر یا username کاربر
    ناخواسته مجازات نمی‌شود.
    """
    username = _norm(getattr(user, "username", None))
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    name = _norm(f"{first} {last}".strip())

    for value in (username, name):
        if not value:
            continue

        for emoji in _REQUIRED_EMOJIS:
            if emoji in value:
                return emoji

        # الگوهای جدید خودشان تکرار حرف و فاصلهٔ بین هر دو حرف را می‌پذیرند.
        for term, pattern in _ROBUST_PATTERNS:
            if pattern.search(value):
                return term

        # سازگاری کامل با فهرست قبلی؛ هر دو شکل عادی و collapse‌شده.
        for candidate in (value, _collapse(value)):
            for pattern in _LEGACY_PATTERNS:
                if pattern.search(candidate):
                    return pattern.pattern

    return custom_filter_reason(chat_id, user)
