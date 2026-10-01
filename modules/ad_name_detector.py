"""تشخیص مستقل نام‌های تبلیغاتی؛ جدا از فیلتر متن گروه.

این ماژول همان فیلتر قدیمی نام تبلیغاتی است، با یک لایهٔ نرمال‌سازی قوی‌تر
برای نویسه‌های هم‌ارز فارسی/عربی، نیم‌فاصله، کشیده، حروف تکراری و جداکننده‌ها.
الگوهای کوتاه و عمومی مرزبندی می‌شوند تا بخشی از یک نام/واژهٔ سالم باعث
false-positive نشود؛ دو عبارت پرخطر «سکس» و «بیوگرافی» عمداً در حالت‌های
چسبیده و شکسته نیز تشخیص داده می‌شوند.
"""
import re
import unicodedata

from modules.user_display import format_user


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


def _norm(value):
    """فرم امن مقایسه با حفظ فقط حروف/عدد و ایموجی‌های ممنوع.

    همهٔ جداکننده‌ها (فاصله، نیم‌فاصله، نقطه، خط تیره، ایموجی دیگر و ...)
    به یک فاصله تبدیل می‌شوند. حرکات، variation selector، کنترل‌های bidi،
    zero-width و کشیده حذف می‌شوند تا نتوانند داخل یک حرف پنهان بمانند.
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
        if category[0] in {"L", "N"}:
            out.append(char)
        else:
            out.append(" ")
    return " ".join("".join(out).split())


def _collapse(value):
    """جمع کردن حروف تکراری: «بییییوگرافی» → «بیوگرافی»."""
    return re.sub(r"(.)\1+", r"\1", value)


def _literal_pattern(term, embedded=False):
    """regex یک عبارت با فاصلهٔ اختیاری و تکرار اختیاری هر حرف."""
    compact = _norm(term).replace(" ", "")
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


def display_name(user):
    return format_user(user)


def reason(user):
    """دلیل تشخیص را برای username یا نام نمایشی فعلی برمی‌گرداند."""
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
    return None
