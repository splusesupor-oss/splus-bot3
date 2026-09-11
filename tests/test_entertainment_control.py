"""تست قابلیت «کنترل سرگرمی به تفکیک گروه» (روشن/خاموش بازی‌های داخلی).

پوشش:
- پیش‌فرض هر گروه فعال است.
- disable / enable / reset درست کار می‌کنند و بین گروه‌ها ایزوله‌اند.
- ماندگاری روی دیسک (بعد از پاک کردن کش).
- فایل خراب یا غایب → پیش‌فرض فعال، بدون exception.
- شناسه‌های مختلفِ یک گروه به یک کلید نرمال می‌شوند.
- offset/length بولد دقیقاً روی «سرگرمی فعال» (با برش UTF-16).
- بولد از نوع entity است و متن هیچ * یا ** ندارد.
- نرمال‌سازی «چهار گزینه‌ای» / «چهار گزینه ای».
- وقتی خاموش است هر دستور بازی مسدود می‌شود؛ وقتی فعال است هیچ‌کدام.
- دستورهای غیربازی (پیام عادی، دستور ادمین، «لیست بازی») هرگز مسدود نمی‌شوند.
- مسدود شدن هیچ عارضه‌ای ندارد (نه state بازی، نه سکه).
- بدون دسترسی، وضعیت تغییر نمی‌کند.

    python tests/test_entertainment_control.py
"""
import asyncio
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ⚠️ پیش از import ماژول‌های بازی/هندلر: economy مسیر فایل خودش را می‌بندد.
import economy.storage as _storage
_storage.use_file(Path(tempfile.mkdtemp()) / "economy.json")

import modules.entertainment_control as ec
import handlers.fox_games_router as router
from modules.fox_games import survival
from modules.riddles import get_token as get_riddle_token
import modules.admin_tools as at
import handlers.message_handler as mh

PASSED = FAILED = 0
CHAT = -777000888


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label} {detail}")
        raise AssertionError(f"{label} {detail}")


def _fresh():
    """فایل ذخیره‌سازی تازه + پاک کردن کش، برای هر تست."""
    ec.use_file(Path(tempfile.mkdtemp()) / "entertainment_mode.json")


def _utf16_slice(text, offset, length):
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


class RecEvent:
    """event قلابی که متن و kwargs (از جمله formatting_entities) را ثبت می‌کند."""

    def __init__(self):
        self.calls = []

    async def reply(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return None


# ---------------------------------------------------------------------------
# ذخیره‌سازی
# ---------------------------------------------------------------------------
def test_default_enabled():
    print("\n### پیش‌فرض: هر گروه فعال است")
    _fresh()
    check("گروه تنظیم‌نشده فعال است", ec.is_enabled(CHAT) is True)
    check("گروه دیگر هم فعال است", ec.is_enabled(-555555) is True)


def test_disable_enable_reset_isolated():
    print("\n### disable / enable / reset + ایزوله بین گروه‌ها")
    _fresh()
    group_a, group_b = -100111, -100222
    ec.disable(group_a)
    check("گروه A خاموش شد", ec.is_enabled(group_a) is False)
    check("گروه B دست‌نخورده (فعال)", ec.is_enabled(group_b) is True)

    ec.enable(group_a)
    check("گروه A دوباره فعال شد", ec.is_enabled(group_a) is True)

    ec.disable(group_a)
    ec.reset(group_a)
    check("reset به پیش‌فرض فعال برگشت", ec.is_enabled(group_a) is True)


def test_persistence_after_cache_clear():
    print("\n### ماندگاری روی دیسک بعد از پاک کردن کش")
    _fresh()
    ec.disable(CHAT)
    ec.reset_cache()
    check("بعد از پاک شدن کش، وضعیت خاموش خوانده شد",
          ec.is_enabled(CHAT) is False)


def test_corrupt_or_missing_file_defaults_enabled():
    print("\n### فایل خراب / غایب → فعال، بدون exception")
    _fresh()
    path = ec._file()
    path.write_text("{ not valid json !!!", encoding="utf-8")
    ec.reset_cache()
    check("فایل خراب → فعال", ec.is_enabled(CHAT) is True)

    _fresh()
    path = ec._file()
    path.unlink(missing_ok=True)
    ec.reset_cache()
    check("فایل غایب → فعال", ec.is_enabled(CHAT) is True)

    path.write_text('"just a string"', encoding="utf-8")
    ec.reset_cache()
    check("فایل غیر-dict → فعال", ec.is_enabled(CHAT) is True)


def test_group_id_normalization():
    print("\n### شناسه‌های مختلف یک گروه → یک کلید")
    _fresh()
    short = 1234567890
    long = -(1_000_000_000_000 + short)  # همان گروه به شکل -100…
    ec.disable(long)
    check("شکل -100… و شکل کوتاه یک رکوردند", ec.is_enabled(short) is False)
    ec.enable(short)
    check("enable از شکل کوتاه، شکل بلند را هم فعال کرد",
          ec.is_enabled(long) is True)


# ---------------------------------------------------------------------------
# بولد واقعی
# ---------------------------------------------------------------------------
def test_blocked_bold_span_utf16():
    print("\n### بولد فقط روی «سرگرمی فعال» — با برش UTF-16")
    span = ec.blocked_bold_span()
    assert span is not None
    offset, length = span
    sliced = _utf16_slice(ec.BLOCKED_NOTICE, offset, length)
    check("برش UTF-16 دقیقاً «سرگرمی فعال» است", sliced == "سرگرمی فعال", repr(sliced))
    check("هیچ ستاره‌ای در متن مسدودی نیست", "*" not in ec.BLOCKED_NOTICE)


def test_full_bold_span_covers_whole_text():
    print("\n### بولدِ کل پیام (خاموش/فعال)")
    for text in (ec.DISABLED_NOTICE, ec.ENABLED_NOTICE):
        offset, length = ec.full_bold_span(text)
        check(f"کل متن بولد شد: {text[:12]}…",
              _utf16_slice(text, offset, length) == text)


def test_reply_uses_real_bold_entity():
    print("\n### بولد از نوع entity است (نه **)")

    async def run():
        ev = RecEvent()
        await ec.send_blocked_notice(ev)
        return ev

    ev = asyncio.run(run())
    check("یک پیام فرستاده شد", len(ev.calls) == 1, str(ev.calls))
    text, kwargs = ev.calls[0]
    check("متن دقیقاً پیام مسدودی است", text == ec.BLOCKED_NOTICE)
    entities = kwargs.get("formatting_entities")
    check("formatting_entities ارسال شد", entities is not None)
    check("یک entity بولد", len(entities or []) == 1)
    entity = (entities or [None])[0]
    check("entity از نوع MessageEntityBold است",
          type(entity).__name__ == "MessageEntityBold",
          str(type(entity)))
    offset, length = entity.offset, entity.length
    check("entity روی «سرگرمی فعال» نشسته",
          _utf16_slice(text, offset, length) == "سرگرمی فعال")
    check("متن هیچ * یا ** ندارد", "*" not in text)


def test_toggle_notices_are_full_bold():
    print("\n### پیام‌های خاموش/فعال با بولدِ کل متن")

    async def run(sender):
        ev = RecEvent()
        await sender(ev)
        return ev

    for sender, expected in (
        (ec.send_disabled_notice, ec.DISABLED_NOTICE),
        (ec.send_enabled_notice, ec.ENABLED_NOTICE),
    ):
        ev = asyncio.run(run(sender))
        text, kwargs = ev.calls[0]
        entities = kwargs.get("formatting_entities") or []
        check(f"متن درست است: {text[:10]}…", text == expected)
        check("یک entity بولدِ تمام‌متن", len(entities) == 1)
        if entities:
            check("بولد کل متن را پوشش می‌دهد",
                  _utf16_slice(text, entities[0].offset, entities[0].length) == text)


# ---------------------------------------------------------------------------
# نرمال‌سازی و تشخیص دستور بازی
# ---------------------------------------------------------------------------
def test_normalization_variants():
    print("\n### نرمال‌سازی دستورهای بازی")
    check("«چهار گزینه‌ای» → «چهار گزینه ای»",
          ec.normalize("چهار گزینه‌ای") == "چهار گزینه ای")
    check("«چهار گزینه ای» → خودش",
          ec.normalize("چهار گزینه ای") == "چهار گزینه ای")
    check("«حدس ايموجي» → «حدس ایموجی»",
          ec.normalize("حدس ايموجي") == "حدس ایموجی")
    check("«مين ياب» (عربی) → «مین یاب»",
          ec.normalize("مين ياب") == "مین یاب")
    check("نیم‌فاصله حذف می‌شود",
          ec.normalize("خون‌آشام") == "خون آشام")


def test_game_commands_detected():
    print("\n### تشخیص دستورهای بازی")
    for command in (
        "چیستان", "حدس ایموجی", "حدس پرچم", "اسم فامیل", "جای خالی",
        "دروغ یا حقیقت", "کی بیشتر بلده", "چهار گزینه ای", "جک",
        "تصحیح کلمات", "جرعت", "حقیقت", "بقا", "معما", "نبرد",
        "خون آشام", "مین یاب", "بخند یا بباز", "حدس جمله", "کارگاه",
    ):
        check(f"دستور بازی شناخته می‌شود: {command}",
              ec.is_game_command(command) is True)


def test_fox_commands_covered_by_guard():
    print("\n### همهٔ دستورهای روتر Fox در گارد هستند")
    missing = [
        command for command in router.FOX_GAME_COMMANDS
        if not ec.is_game_command(command)
    ]
    check("FOX_GAME_COMMANDS ⊆ GAME_COMMANDS", not missing, str(missing))


def test_non_game_never_blocked():
    print("\n### غیربازی‌ها هرگز مسدود نمی‌شوند")
    _fresh()
    ec.disable(CHAT)
    for text in (
        "لیست بازی", "لیست بازی ها", "لیست بازی‌ها",
        "سلام", "ثبت ادمین @ali", "!addword تبلیغ", "سایت بازی",
        "راهنما", "موجودی", "آمارم",
    ):
        check(f"«{text}» مسدود نمی‌شود", ec.blocks(CHAT, text) is False)
        check(f"«{text}» دستور بازی نیست", ec.is_game_command(text) is False)


def test_blocks_only_when_disabled():
    print("\n### blocks فقط وقتی خاموش است")
    _fresh()
    check("فعال → بازی مسدود نیست", ec.blocks(CHAT, "چیستان") is False)
    ec.disable(CHAT)
    check("خاموش → بازی مسدود می‌شود", ec.blocks(CHAT, "چیستان") is True)
    check("خاموش → پیام عادی مسدود نمی‌شود", ec.blocks(CHAT, "سلام") is False)
    check("خاموش → «لیست بازی» مسدود نمی‌شود",
          ec.blocks(CHAT, "لیست بازی") is False)
    ec.enable(CHAT)
    check("فعالِ دوباره → بازی مسدود نیست", ec.blocks(CHAT, "چیستان") is False)


def test_guard_sends_notice_and_returns_true():
    print("\n### guard: هشدار می‌فرستد و True برمی‌گرداند")

    async def run():
        _fresh()
        ec.disable(CHAT)
        ev = RecEvent()
        result = await ec.guard(ev, CHAT, "چیستان")
        return result, ev

    result, ev = asyncio.run(run())
    check("گارد True برگرداند", result is True)
    check("فقط یک پیام هشدار فرستاد", len(ev.calls) == 1, str(ev.calls))
    check("متن هشدار درست است", ev.calls[0][0] == ec.BLOCKED_NOTICE)


def test_guard_returns_false_when_allowed():
    print("\n### guard وقتی مجاز است False برمی‌گرداند")

    async def run():
        _fresh()
        ev = RecEvent()
        allowed = await ec.guard(ev, CHAT, "چیستان")   # فعال
        _fresh()
        ec.disable(CHAT)
        ev2 = RecEvent()
        normal = await ec.guard(ev2, CHAT, "سلام")     # غیربازی
        return allowed, ev, normal, ev2

    allowed, ev, normal, ev2 = asyncio.run(run())
    check("فعال → guard اجازه می‌دهد", allowed is False)
    check("فعال → پیامی نفرستاد", len(ev.calls) == 0, str(ev.calls))
    check("خاموش + غیربازی → اجازه می‌دهد", normal is False)
    check("خاموش + غیربازی → پیامی نفرستاد", len(ev2.calls) == 0, str(ev2.calls))


# ---------------------------------------------------------------------------
# لایهٔ دوم در روتر بازی‌ها — بدون عارضه
# ---------------------------------------------------------------------------
def test_router_layer2_blocks_before_state():
    print("\n### گارد لایهٔ دوم روتر: نه state، نه تایمر، نه سکه")

    class FakeBot:
        def __init__(self):
            self.award_calls = []

        def award_coins(self, chat_id, user_id, name, amount):
            self.award_calls.append((chat_id, user_id, amount))
            return 0

    class Logger:
        def log_info(self, m):
            pass

        def log_error(self, m):
            pass

    async def run():
        _fresh()
        ec.disable(CHAT)
        bot = FakeBot()
        ev = RecEvent()
        consumed = await router.handle(bot, ev, CHAT, 4242, None, "بقا", Logger())
        return consumed, ev, bot

    consumed, ev, bot = asyncio.run(run())
    check("روتر پیام را مصرف کرد (True)", consumed is True)
    check("هشدار مسدودی فرستاد", any(c[0] == ec.BLOCKED_NOTICE for c in ev.calls),
          str(ev.calls))
    check("هیچ state بازی بقا ساخته نشد", survival.is_active(CHAT) is False)
    check("هیچ سکه‌ای پرداخت نشد", bot.award_calls == [], str(bot.award_calls))


# ---------------------------------------------------------------------------
# مسیر واقعی هندلر اصلی — توگل و گارد
# ---------------------------------------------------------------------------
class _Noop:
    def __call__(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        return _NOOP_INSTANCE


_NOOP_INSTANCE = _Noop()


class _Logger:
    def __init__(self):
        self.info, self.errors = [], []

    def log_info(self, message):
        self.info.append(message)

    def log_error(self, message):
        self.errors.append(message)


class _ConfigManager:
    def get(self, key, default=None):
        return default


class _Tracker:
    def get_count(self, chat, user):
        return 0

    def is_banned(self, chat, user):
        return False

    def is_muted(self, chat, user):
        return False

    def increment(self, chat, user):
        return 1

    def decrement(self, chat, user):
        return 0

    def reset_count(self, chat, user):
        return None

    def should_punish(self, chat, user):
        return False


class _Detector:
    def is_spam(self, *args, **kwargs):
        return False, None

    def check_message(self, *args, **kwargs):
        return False, None

    def check_banned_words(self, *args, **kwargs):
        return False, None

    def has_public_username(self, *args, **kwargs):
        return False


class _User:
    def __init__(self, uid, name="علی", username=None):
        self.id = uid
        self.first_name = name
        self.last_name = None
        self.username = username


class _Message:
    _n = 1000

    def __init__(self, text):
        _Message._n += 1
        self.message = text
        self.id = _Message._n
        self.file = None


class _Event:
    def __init__(self, text, user_id, chat_id=CHAT):
        self.message = _Message(text)
        self.chat_id = chat_id
        self.is_private = False
        self.replies = []
        self._user = _User(user_id, username="owner")
        self.reply_to = None

    async def get_chat(self):
        return types.SimpleNamespace(id=self.chat_id)

    async def get_sender(self):
        return self._user

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def respond(self, text, **kwargs):
        self.replies.append(text)


class _Client:
    async def get_messages(self, *args, **kwargs):
        return []

    async def delete_messages(self, *args, **kwargs):
        return None

    async def send_message(self, target, text, **kwargs):
        return True

    async def get_permissions(self, *args, **kwargs):
        return None

    async def __call__(self, *args, **kwargs):
        return None


class _FakeBot:
    def __init__(self):
        self.logger = _Logger()
        self.config_manager = _ConfigManager()
        self.tracker = _Tracker()
        self.detector = _Detector()
        self.client = _Client()
        self.group_timer_tasks = {}
        self.bot_account_id = 555
        self.punished_users = set()
        self.spam_burst_messages = {}
        self.spammer_messages = {}
        self.spam_burst_users = set()
        self.moderation_queue = types.SimpleNamespace(enqueue=lambda *a, **k: True)
        self.admin_actions = types.SimpleNamespace()
        self.group_actions = types.SimpleNamespace(
            lock_group=lambda *a, **k: None,
            unlock_group=lambda *a, **k: None,
        )
        self.cleanup_tasks = {}
        # containerهایی که هندلر با getattr(..., default) می‌خواند
        self.reply_input_peer_cache = {}
        self.native_admin_state_cleared = set()
        self._big_spam_incidents = {}
        self.spam_lock = set()
        self.gif_spam_notification_until = {}
        self.outgoing_sender = None
        self.message_delete_queue = None
        self.notice_cleanup = None

    def __getattr__(self, name):
        return _NOOP_INSTANCE


def _patch_perm(value):
    original = at.has_admin_permission
    at.has_admin_permission = lambda chat_id, user_id, username=None: value
    return original


def _restore_perm(original):
    at.has_admin_permission = original


def _drive(text, user_id, is_private=False, perm=True, chat_id=CHAT):
    bot = _FakeBot()
    event = _Event(text, user_id, chat_id=chat_id)
    event.is_private = is_private
    original = _patch_perm(perm)
    try:
        asyncio.run(mh.handle_new_message(bot, event))
    finally:
        _restore_perm(original)
    return event, bot


def test_handler_toggle_owner_disable_enable():
    print("\n### هندلر واقعی: مالک خاموش/روشن می‌کند")
    _fresh()
    ev1, _bot = _drive("سرگرمی خاموش", 111, perm=True)
    check("پیام خاموشی ارسال شد",
          any(ec.DISABLED_NOTICE in r for r in ev1.replies), str(ev1.replies))
    check("وضعیت واقعاً خاموش شد", ec.is_enabled(CHAT) is False)

    ev2, _bot2 = _drive("سرگرمی فعال", 111, perm=True)
    check("پیام فعال‌سازی ارسال شد",
          any(ec.ENABLED_NOTICE in r for r in ev2.replies), str(ev2.replies))
    check("وضعیت واقعاً فعال شد", ec.is_enabled(CHAT) is True)


def test_handler_toggle_denied_for_regular_user():
    print("\n### هندلر واقعی: کاربر عادی رد می‌شود و وضعیت تغییر نمی‌کند")
    _fresh()
    ev, _bot = _drive("سرگرمی خاموش", 99999999, perm=False)
    check("پیام رد دسترسی ارسال شد",
          any(ec.PERMISSION_DENIED in r for r in ev.replies), str(ev.replies))
    check("وضعیت تغییر نکرد (هنوز فعال)", ec.is_enabled(CHAT) is True)


def test_handler_toggle_private_chat():
    print("\n### هندلر واقعی: در چت خصوصی رد می‌شود")
    _fresh()
    ev, _bot = _drive("سرگرمی خاموش", 111, is_private=True, perm=True)
    check("پیام «فقط داخل گروه» ارسال شد",
          any(ec.PRIVATE_ONLY in r for r in ev.replies), str(ev.replies))
    check("وضعیت تغییر نکرد (هنوز فعال)", ec.is_enabled(CHAT) is True)


def test_handler_game_blocked_when_disabled():
    print("\n### هندلر واقعی: بازی خاموش از مسیر اصلی مسدود می‌شود")
    _fresh()
    ec.disable(CHAT)
    ev, _bot = _drive("چیستان", 222, perm=False)
    check("پیام مسدودی ارسال شد",
          any(ec.BLOCKED_NOTICE in r for r in ev.replies), str(ev.replies))
    check("هیچ state چیستان ساخته نشد",
          get_riddle_token(CHAT, 222) is None)
    ec.reset(CHAT)


def main():
    tests = [
        test_default_enabled,
        test_disable_enable_reset_isolated,
        test_persistence_after_cache_clear,
        test_corrupt_or_missing_file_defaults_enabled,
        test_group_id_normalization,
        test_blocked_bold_span_utf16,
        test_full_bold_span_covers_whole_text,
        test_reply_uses_real_bold_entity,
        test_toggle_notices_are_full_bold,
        test_normalization_variants,
        test_game_commands_detected,
        test_fox_commands_covered_by_guard,
        test_non_game_never_blocked,
        test_blocks_only_when_disabled,
        test_guard_sends_notice_and_returns_true,
        test_guard_returns_false_when_allowed,
        test_router_layer2_blocks_before_state,
        test_handler_toggle_owner_disable_enable,
        test_handler_toggle_denied_for_regular_user,
        test_handler_toggle_private_chat,
        test_handler_game_blocked_when_disabled,
    ]
    for test in tests:
        try:
            test()
        except AssertionError as error:
            print(f"  !! {test.__name__}: {error}")
        except Exception as error:  # noqa: BLE001
            import traceback
            print(f"  !! {test.__name__} raised: {error}")
            traceback.print_exc()

    print("\n" + "=" * 56)
    print(f"passed={PASSED} failed={FAILED}")
    print("=" * 56)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
