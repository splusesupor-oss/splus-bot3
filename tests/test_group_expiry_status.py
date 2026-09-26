"""⏳ «مهلت گروه» + همگام‌سازی «لیست انقضا» — تست کامل و مستقل.

پوشش:
  • قالب دقیق پیام «مهلت گروه» و اینکه فقط دو برچسب Bold هستند.
  • محاسبهٔ مهلت از رکورد واقعی همان گروه (نه مقدار ثابت/قدیمی).
  • دسترسی: مالک اصلی، مالک گروه، ادمین ثبت‌شده — و رد شدن بقیه.
  • تغییر نام گروه: به‌روزرسانی نام بدون ساختن گروه جدید یا از دست
    رفتن اشتراک.
  • ماندگاری پس از ری‌استارت و دقت لحظهٔ انقضا.
  • همگام‌سازی «لیست انقضا»: حذف رکوردهای بی‌گروه، وضعیت گروه
    تمدیدشده، و ایمنی در برابر groups.json خالی.

اجرا:
    python -m pytest tests/test_group_expiry_status.py -q
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import handlers.group_expiry_handler as geh
import modules.admin_storage as admin_storage
import modules.group_expiry as ge
import modules.group_storage as group_storage
import modules.owner_check as owner_check
from modules.expiry_report import build_group_list, build_report

OWNER_ID = 555000111
GROUP_OWNER_ID = 777000222
ADMIN_ID = 888000333
OUTSIDER_ID = 999000444
CHAT = -1001234567890
SHORT_CHAT = 1234567890
KEY = "1234567890"


# ---------------------------------------------------------------------------
# ابزار تست
# ---------------------------------------------------------------------------
class User:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username
        self.first_name = "U"
        self.last_name = None


class Chat:
    def __init__(self, title="گروه روباه"):
        self.title = title


class Logger:
    def __init__(self):
        self.lines = []

    def log_info(self, message):
        self.lines.append(str(message))

    def log_error(self, message):
        self.lines.append(str(message))

    def has(self, needle):
        return any(needle in line for line in self.lines)


class Client:
    def __init__(self):
        self.sent = []

    async def send_message(self, target, text, **kwargs):
        self.sent.append((target, text, kwargs.get("formatting_entities")))
        return object()


class Bot:
    def __init__(self):
        self.client = Client()
        self.logger = Logger()
        self.notice_cleanup = None


class Event:
    def __init__(self, title="گروه روباه"):
        self.out = []
        self.entities = []
        self.chat = Chat(title) if title else None

    async def reply(self, text, formatting_entities=None, **kwargs):
        self.out.append(text)
        self.entities.append(formatting_entities or [])
        return object()

    async def get_chat(self):
        return self.chat


def decode(text, offset, length):
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le")


@pytest.fixture
def stores(tmp_path, monkeypatch):
    """هر تست روی فایل‌های تازهٔ خودش کار می‌کند؛ هیچ state واقعی دست نمی‌خورد."""
    monkeypatch.setattr(ge, "FILE", tmp_path / "group_expiry.json")
    monkeypatch.setattr(group_storage, "FILE", tmp_path / "groups.json")
    monkeypatch.setattr(admin_storage, "FILE", tmp_path / "admins.json")
    for module in (ge, group_storage, admin_storage):
        module._cache = None
        module._cache_mtime = None
    monkeypatch.setattr(geh, "_title_memo", {})
    monkeypatch.setattr(geh, "_title_probed", set())
    monkeypatch.setattr(
        owner_check, "get_owner",
        lambda: {"user_id": OWNER_ID, "username": None},
    )
    group_storage.save_groups({
        KEY: {"title": "گروه روباه", "active": True},
    })
    admin_storage.FILE.write_text(json.dumps({
        KEY: [{"user_id": ADMIN_ID}],
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def register_group(owner_id=GROUP_OWNER_ID, title="گروه روباه", active=True):
    group_storage.save_groups({
        KEY: {"title": title, "active": active, "owner_id": owner_id},
    })
    group_storage._cache = None
    group_storage._cache_mtime = None


# ---------------------------------------------------------------------------
# ۱) قالب دقیق پیام
# ---------------------------------------------------------------------------
def test_status_message_is_exact_and_only_labels_are_bold(stores):
    text, spans = ge.build_status_message("گروه روباه", "۳ روز و ۶ ساعت")
    assert text == (
        "↻- گروه : گروه روباه\n"
        "مهلت باقی مانده : ۳ روز و ۶ ساعت\n"
        "برای تمدید اشتراک : 𝄞 @aifox_bot"
    )
    # فقط دو برچسب Bold و هیچ نقل‌قول شیشه‌ای.
    assert [kind for kind, _, _ in spans] == ["bold", "bold"]
    fragments = [decode(text, offset, length) for _, offset, length in spans]
    assert fragments == ["گروه", "مهلت باقی مانده"]


def test_status_bold_labels_are_correct_even_when_title_contains_word(stores):
    """اگر نام گروه خودش «گروه» داشته باشد، فقط برچسب Bold می‌شود."""
    text, spans = ge.build_status_message("گروه دوستان گروه", "۱ روز و ۰ ساعت")
    fragments = [decode(text, offset, length) for _, offset, length in spans]
    assert fragments == ["گروه", "مهلت باقی مانده"]
    # برچسب اول دقیقاً بعد از پیشوند است، نه داخل نام گروه.
    assert text.startswith("↻- گروه : گروه دوستان گروه")


# ---------------------------------------------------------------------------
# ۲) مهلت از رکورد واقعی همان گروه محاسبه می‌شود
# ---------------------------------------------------------------------------
def test_remaining_is_computed_from_the_real_record(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, ge.ONE_WEEK, now=now - timedelta(days=3, hours=6))
    assert ge.remaining_text(CHAT, now=now) == "۳ روز و ۱۸ ساعت"

    # تمدید: مقدار باید فوراً از رکورد جدید بیاید، نه مقدار قبلی.
    ge.set_expiry(CHAT, ge.ONE_MONTH, now=now)
    assert ge.remaining_text(CHAT, now=now) == "۲۹ روز و ۰ ساعت"


def test_remaining_shows_expired_and_not_registered(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=6))
    assert ge.remaining_text(CHAT, now=now) == "منقضی شده"
    assert ge.remaining_text(-1009999999999, now=now) == "ثبت نشده"


def test_remaining_minutes_only(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=5) + timedelta(minutes=42))
    assert ge.remaining_text(CHAT, now=now) == "۴۲ دقیقه"


# ---------------------------------------------------------------------------
# ۳) دسترسی: مالک اصلی / مالک گروه / ادمین — نه بقیه
# ---------------------------------------------------------------------------
def _run_status(uid, title="گروه روباه", allow=None):
    bot, event = Bot(), Event(title)
    consumed = asyncio.run(geh.handle_status(
        bot, event, CHAT, User(uid), "مهلت گروه", bot.logger, allow=allow))
    return consumed, event, bot


def test_registered_admin_gets_the_status(stores):
    register_group()
    consumed, event, bot = _run_status(ADMIN_ID)
    assert consumed is True
    assert len(event.out) == 1
    assert event.out[0].startswith("↻- گروه : گروه روباه")
    assert bot.logger.has("GROUP EXPIRY STATUS SENT")


def test_group_owner_gets_the_status(stores):
    register_group()
    consumed, event, _bot = _run_status(GROUP_OWNER_ID)
    assert consumed is True
    assert len(event.out) == 1


def test_global_owner_gets_the_status(stores):
    register_group()
    consumed, event, _bot = _run_status(OWNER_ID)
    assert consumed is True
    assert len(event.out) == 1


def test_outsider_is_silently_refused(stores):
    register_group()
    consumed, event, bot = _run_status(OUTSIDER_ID)
    assert consumed is True          # پیام مصرف می‌شود و جلوتر نمی‌رود
    assert event.out == []           # ولی هیچ پاسخی نمی‌گیرد
    assert bot.logger.has("reason=not_admin_or_owner")


def test_caller_supplied_admin_flag_is_respected(stores):
    """ادمینِ خودِ سروش (native) هم با allow=True پاسخ می‌گیرد."""
    register_group()
    consumed, event, _bot = _run_status(OUTSIDER_ID, allow=True)
    assert consumed is True
    assert len(event.out) == 1


def test_only_exact_command_matches(stores):
    assert ge.match_status_command("مهلت گروه") is True
    assert ge.match_status_command(" مهلت  گروه ") is True
    assert ge.match_status_command("مهلت گروه من") is False
    assert ge.match_status_command("گرفتن مهلت گروه") is False
    assert ge.match_status_command("لیست انقضا") is False


async def _status_returns_false_for_other_text():
    bot, event = Bot(), Event()
    for text in ("سلام", "لیست انقضا", "یک هفته", "مهلت گروه من"):
        consumed = await geh.handle_status(
            bot, event, CHAT, User(ADMIN_ID), text, bot.logger)
        assert consumed is False, text
    assert event.out == []


def test_handler_ignores_other_text(stores):
    register_group()
    asyncio.run(_status_returns_false_for_other_text())


# ---------------------------------------------------------------------------
# ۴) تغییر نام گروه
# ---------------------------------------------------------------------------
def test_rename_updates_both_stores_without_new_group(stores):
    register_group(title="نام قدیمی")
    ge.set_expiry(CHAT, ge.ONE_WEEK, title="نام قدیمی")

    assert geh.sync_group_title(CHAT, "نام جدید") is True
    # نام در هر دو storage به‌روز شد.
    assert group_storage.load_groups()[KEY]["title"] == "نام جدید"
    assert ge.get_record(CHAT)["title"] == "نام جدید"
    # فقط یک گروه وجود دارد؛ تغییر نام گروه جدید نساخت.
    assert list(group_storage.load_groups()) == [KEY]
    assert list(ge.all_records()) == [KEY]
    # اشتراک دست‌نخورده باقی ماند.
    assert ge.has_expiry(CHAT) is True
    # تکرار همان نام، نوشتن اضافی نمی‌کند.
    assert geh.sync_group_title(CHAT, "نام جدید") is False


def test_status_command_uses_the_live_group_name(stores):
    """با اولین پیام بعد از تغییر نام، نام جدید گرفته و ذخیره می‌شود."""
    register_group(title="نام قدیمی")
    ge.set_expiry(CHAT, ge.ONE_WEEK, title="نام قدیمی")

    consumed, event, bot = _run_status(ADMIN_ID, title="نام تازه")
    assert consumed is True
    assert event.out[0].startswith("↻- گروه : نام تازه")
    assert group_storage.load_groups()[KEY]["title"] == "نام تازه"
    assert ge.get_record(CHAT)["title"] == "نام تازه"
    assert bot.logger.has("GROUP EXPIRY TITLE UPDATED")


class ProbingEvent(Event):
    """رویدادی که chat کش‌شده ندارد و عنوان را با RPC می‌دهد."""

    def __init__(self, title="گروه از راه RPC"):
        super().__init__(title=None)
        self.rpc_calls = 0
        self._rpc_title = title

    async def get_chat(self):
        self.rpc_calls += 1
        return Chat(self._rpc_title)


def test_title_sync_prefers_event_cache_and_needs_no_rpc(stores):
    register_group(title="نام قدیمی")
    event = ProbingEvent()
    event.chat = Chat("نام داخل کش")
    assert asyncio.run(
        geh.sync_title_from_event(event, CHAT, Logger())) is True
    assert event.rpc_calls == 0
    assert group_storage.load_groups()[KEY]["title"] == "نام داخل کش"


def test_title_sync_fetches_by_rpc_only_once_per_group(stores):
    """اگر رویداد عنوان نداشت، فقط یک‌بار RPC زده می‌شود."""
    register_group(title="نام قدیمی")
    first = ProbingEvent("نام تازه")
    assert asyncio.run(geh.sync_title_from_event(first, CHAT, Logger())) is True
    assert first.rpc_calls == 1
    assert group_storage.load_groups()[KEY]["title"] == "نام تازه"

    second = ProbingEvent("نام تازه‌تر")
    # عنوانِ کش‌شدهٔ رویداد دوم هم خالی است، ولی RPC تکرار نمی‌شود.
    assert asyncio.run(
        geh.sync_title_from_event(second, CHAT, Logger())) is False
    assert second.rpc_calls == 0
    assert group_storage.load_groups()[KEY]["title"] == "نام تازه"


def test_title_sync_survives_rpc_failure(stores):
    register_group(title="نام قدیمی")

    class BrokenEvent(ProbingEvent):
        async def get_chat(self):
            self.rpc_calls += 1
            raise RuntimeError("no peer")

    event = BrokenEvent()
    assert asyncio.run(geh.sync_title_from_event(event, CHAT, Logger())) is False
    assert group_storage.load_groups()[KEY]["title"] == "نام قدیمی"


def test_rename_never_creates_an_expiry_record(stores):
    """گروهی که اشتراک ندارد با تغییر نام رکورد انقضا نمی‌گیرد."""
    register_group(title="بدون اشتراک")
    assert geh.sync_group_title(CHAT, "نام جدید") is True
    assert ge.get_record(CHAT) is None
    assert ge.has_expiry(CHAT) is False


def test_group_is_identified_by_its_id(stores):
    """شکل کوتاه و شکل -100 شناسه، یک گروه‌اند."""
    ge.set_expiry(CHAT, ge.ONE_WEEK)
    assert ge.has_expiry(SHORT_CHAT) is True
    assert ge.get_record(SHORT_CHAT) == ge.get_record(CHAT)
    assert list(ge.all_records()) == [KEY]


# ---------------------------------------------------------------------------
# ۵) ماندگاری، دقت انقضا و خاموش شدن گروه
# ---------------------------------------------------------------------------
def test_status_survives_restart(stores):
    ge.set_expiry(CHAT, ge.TWO_WEEKS, title="گروه روباه")
    # ری‌استارت: کش‌ها خالی و خواندن از فایل.
    ge._cache = ge._cache_mtime = None
    group_storage._cache = group_storage._cache_mtime = None

    expected = ge.remaining_text(CHAT)
    assert expected not in (ge.NO_EXPIRY_TEXT, ge.EXPIRED_STATUS_TEXT)

    consumed, event, _bot = _run_status(ADMIN_ID)
    assert consumed is True
    assert expected in event.out[0]


def test_expiry_is_exact_and_blocks_every_command(stores):
    """دقیقاً در لحظهٔ انقضا گروه خاموش می‌شود — با ساعت واقعی سنجیده می‌شود."""
    now = datetime.now(timezone.utc)
    # یک ثانیه مانده به انقضا
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=5) + timedelta(seconds=1))
    assert ge.is_expired(CHAT) is False
    assert geh.blocks_message(CHAT, User(ADMIN_ID)) is False

    # دقیقاً سرِ لحظهٔ انقضا (و بعد از آن) همه‌چیز متوقف می‌شود.
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=5))
    assert ge.is_expired(CHAT) is True
    assert geh.blocks_message(CHAT, User(OUTSIDER_ID)) is True
    assert geh.blocks_message(CHAT, User(ADMIN_ID)) is True
    # مالک اصلی عبور می‌کند تا بتواند تمدید کند.
    assert geh.blocks_message(CHAT, User(OWNER_ID)) is False


def test_renewal_reactivates_the_group(stores):
    now = datetime.now(timezone.utc)
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=6))
    assert geh.blocks_message(CHAT, User(ADMIN_ID)) is True
    ge.set_expiry(CHAT, ge.ONE_WEEK, now=now)
    assert ge.is_expired(CHAT) is False
    assert geh.blocks_message(CHAT, User(ADMIN_ID)) is False
    # گروه تمدیدشده دیگر «منقضی شده» نشان داده نمی‌شود.
    assert ge.remaining_text(CHAT, now=now) == "۷ روز و ۰ ساعت"


# ---------------------------------------------------------------------------
# ۵٫۱) کرانِ تلاش برای ارسال پیام انقضا
# ---------------------------------------------------------------------------
def test_notice_attempts_are_counted_and_capped(stores):
    """اگر ارسال پیام انقضا نشود، بعد از ۳ تلاش دیگر تلاشی نمی‌شود."""
    now = datetime.now(timezone.utc)
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=6))
    assert ge.notice_attempts(CHAT) == 0
    assert ge.notice_attempts(CHAT) < ge.MAX_NOTICE_ATTEMPTS

    for expected in (1, 2, 3):
        assert ge.record_notice_attempt(CHAT) == expected
    assert ge.notice_attempts(CHAT) == ge.MAX_NOTICE_ATTEMPTS

    # گروه همچنان منقضی و خاموش است؛ فقط «اعلام» متوقف می‌شود.
    assert ge.is_expired(CHAT) is True
    # اشتراک با شمارندهٔ تلاش دست‌نخورده باقی می‌ماند.
    assert ge.has_expiry(CHAT) is True


def test_renewal_resets_notice_attempts(stores):
    now = datetime.now(timezone.utc)
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=6))
    for _ in range(ge.MAX_NOTICE_ATTEMPTS):
        ge.record_notice_attempt(CHAT)
    assert ge.notice_attempts(CHAT) == ge.MAX_NOTICE_ATTEMPTS

    ge.set_expiry(CHAT, ge.ONE_WEEK, now=now)
    assert ge.notice_attempts(CHAT) == 0
    assert ge.is_expired(CHAT, now=now) is False


def test_check_once_stops_after_the_attempt_limit(stores):
    """ناظر: پس از سقف تلاش، گروه از فهرست اعلام بیرون می‌رود."""
    class FailingClient:
        def __init__(self):
            self.sent = []

        async def send_message(self, target, text, **kwargs):
            raise RuntimeError("flood wait")

    class WatcherBot:
        def __init__(self):
            self.client = FailingClient()
            self.logger = Logger()
            self.notice_cleanup = None

    now = datetime.now(timezone.utc)
    ge.set_expiry(CHAT, ge.FIVE_DAYS, now=now - timedelta(days=6))
    deactivated = []

    for _ in range(ge.MAX_NOTICE_ATTEMPTS):
        bot = WatcherBot()
        asyncio.run(geh.check_once(
            bot, lambda c, t: deactivated.append(c), logger=bot.logger))

    assert ge.was_notified(CHAT) is True
    assert ge.due_groups() == []
    # هر دور گروه را غیرفعال کرد (idempotent) ولی اعلام تکراری ندارد.
    assert len(deactivated) == ge.MAX_NOTICE_ATTEMPTS


# ---------------------------------------------------------------------------
# ۶) همگام‌سازی «لیست انقضا»
# ---------------------------------------------------------------------------
def test_sync_removes_orphan_records_and_refreshes_status(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    # گروه ثبت‌شده با رکورد منقضی‌شده
    ge.set_expiry(1111111111, ge.FIVE_DAYS, now=now - timedelta(days=9),
                  title="گروه منقضی")
    # رکوردِ گروهی که دیگر در groups.json نیست
    ge.set_expiry(2222222222, ge.ONE_WEEK, now=now - timedelta(days=30))
    group_storage.save_groups({
        "1111111111": {"title": "گروه منقضی", "active": False},
    })
    group_storage._cache = group_storage._cache_mtime = None

    summary = geh.sync_expiry_list(Logger())
    assert summary["orphans_removed"] == 1
    assert summary["expired"] == 1
    assert "2222222222" not in ge.all_records()
    assert "1111111111" in ge.all_records()


def test_sync_updates_titles_of_renewed_groups(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    ge.set_expiry(1111111111, ge.ONE_WEEK, now=now - timedelta(days=20),
                  title="نام قدیمی")
    group_storage.save_groups({
        "1111111111": {"title": "نام تازه", "active": True},
    })
    group_storage._cache = group_storage._cache_mtime = None

    summary = geh.sync_expiry_list(Logger())
    assert summary["titles_updated"] == 1
    assert summary["with_expiry"] == 1
    assert ge.get_record(1111111111)["title"] == "نام تازه"
    # تمدید، وضعیت را واقعاً عوض می‌کند.
    ge.set_expiry(1111111111, ge.ONE_MONTH, now=now)
    assert ge.is_expired(1111111111, now=now) is False
    assert "۲۹ روز" in ge.remaining_text(1111111111, now=now)


def test_sync_never_prunes_when_groups_storage_is_empty(stores):
    """groups.json خالی/خراب نباید همهٔ اشتراک‌ها را پاک کند."""
    ge.set_expiry(CHAT, ge.ONE_WEEK)
    group_storage.save_groups({})
    group_storage._cache = group_storage._cache_mtime = None

    summary = geh.sync_expiry_list(Logger())
    assert summary["skipped"] == "no_registered_groups"
    assert summary["orphans_removed"] == 0
    assert ge.has_expiry(CHAT) is True


def test_sync_loop_runs_repeatedly_and_survives_errors(stores, monkeypatch):
    calls = {"count": 0}

    def fake_sync(logger=None):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    monkeypatch.setattr(geh, "sync_expiry_list", fake_sync)
    rounds = asyncio.run(geh.run_expiry_list_sync(
        interval=0, logger=Logger(), iterations=3))
    assert rounds == 3
    assert calls["count"] == 3


# ---------------------------------------------------------------------------
# ۷) گزارش «لیست انقضا» وضعیت واقعی را نشان می‌دهد
# ---------------------------------------------------------------------------
def test_report_shows_renewed_group_with_new_date(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    ge.set_expiry(1111111111, ge.ONE_WEEK, now=now - timedelta(days=30),
                  title="گروه تمدیدشده")
    group_storage.save_groups({
        "1111111111": {"title": "گروه تمدیدشده", "active": True},
    })
    group_storage._cache = group_storage._cache_mtime = None

    stale = build_report(now=now)
    assert "منقضی شده" in stale

    ge.set_expiry(1111111111, ge.ONE_MONTH, now=now)
    fresh = build_report(now=now)
    assert "گروه تمدیدشده" in fresh
    assert "⏳ باقی‌مانده: ۲۹ روز و ۰ ساعت" in fresh
    assert "📅 تاریخ انقضا:" in fresh
    assert "منقضی شده" not in fresh


def test_report_marks_closed_groups_and_group_list_stays_compact(stores):
    now = datetime(2026, 3, 1, 12, tzinfo=timezone.utc)
    ge.set_expiry(1111111111, ge.FIVE_DAYS, now=now - timedelta(days=9),
                  title="گروه خاموش")
    group_storage.save_groups({
        "1111111111": {"title": "گروه خاموش", "active": False},
    })
    group_storage._cache = group_storage._cache_mtime = None

    report = build_report(now=now)
    assert "❌ گروه: گروه خاموش" in report
    assert "🔌 ربات در گروه: خاموش" in report
    assert "🔄 همگام‌سازی:" in report

    listing = build_group_list(now=now)
    assert "1. گروه خاموش" in listing
    assert "منقضی شده" in listing


# ---------------------------------------------------------------------------
# ۸) اتصال به بدنهٔ ربات (بررسی سطح منبع)
#
# handlers/message_handler زیر پایتون ۳٫۱۱ import نمی‌شود (f-string با
# backslash در modules/name_family)، پس اتصال با خواندن خودِ منبع بررسی
# می‌شود تا ترتیب گیت‌ها هم تضمین بماند.
# ---------------------------------------------------------------------------
def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def test_gate_is_wired_before_other_features():
    source = _read("handlers/message_handler.py")
    gate = source.index("GROUP EXPIRY BLOCKED")
    status = source.index("handle_group_expiry_status")
    photo = source.index("_photo_dl.COMMAND")
    button = source.index('clean_text == "تست دکمه"')
    bot_detector = source.index("BOT SHUTDOWN DEBUG")
    # گیت انقضا و دستور «مهلت گروه» پیش از هر قابلیت دیگری بررسی می‌شوند.
    assert status < gate
    assert gate < photo
    assert gate < button
    assert gate < bot_detector
    # همگام‌سازی نام گروه هم در همان گیت انجام می‌شود.
    assert source.index("sync_group_expiry_title") < gate


def test_status_command_is_an_admin_lane_command():
    source = _read("modules/group_dispatch.py")
    admin_block = source[source.index("_ADMIN_EXACT = frozenset({"):]
    admin_block = admin_block[:admin_block.index("})")]
    assert '"مهلت گروه"' in admin_block


def test_help_block_is_bold_and_blockquoted_in_admin_help():
    source = _read("handlers/message_handler.py")
    assert "برای دیدن مهلت باقی مانده گروه" in source
    assert "بنویسید مهلت گروه" in source
    assert "فقط مدیر یا مالک" in source
    block = source.index("expiry_help_block = (")
    # یک بار در متن راهنما، یک بار در فهرست Bold و یک بار در نقل‌قول‌ها.
    assert source.count("expiry_help_block") >= 4
    bold_list = source.index("bold_pieces = [")
    quote_list = source.index("quote_sections = [")
    assert source.index("expiry_help_block,", bold_list) > bold_list
    assert source.index("expiry_help_block]:", quote_list) > quote_list
    assert block < bold_list


def test_expiry_list_sync_is_wired_into_the_bot_loops():
    source = _read("core/bot_working_split_ok.py")
    # حلقهٔ ۲۴ ساعته کنار ناظرِ انقضا ساخته می‌شود.
    loop = source.index("group_expiry_list_sync_loop")
    assert "run_group_expiry_list_sync" in source[loop:loop + 600]
    assert "GROUP_EXPIRY_LIST_SYNC_SECONDS" in source[loop:loop + 600]
    assert source.index("run_group_expiry_watcher") < loop
    # همگام‌سازی هنگام درخواست «لیست انقضا» هم انجام می‌شود: آخرین مسیرِ
    # این دستور (مسیر واقعی پاسخ، نه مسیر لاگ تشخیصی) اول sync می‌کند و
    # بعد گزارش را می‌سازد.
    route = source.rindex('if text == "لیست انقضا"')
    region = source[route:route + 2500]
    assert "sync_group_expiry_list" in region
    assert "EXPIRY LIST SYNC ON DEMAND" in region
    assert region.index("sync_group_expiry_list") < region.index(
        "build_expiry_report(")
