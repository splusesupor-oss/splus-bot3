"""⏳ هندلر مستقل «تاریخ انقضای گروه».

تنها نقطهٔ اتصال این قابلیت به ربات. هیچ state ای اینجا نگه داشته
نمی‌شود و هیچ ماژول بازی/حافظه/قفلی import نمی‌گردد.

مسیر پردازش کاملاً جداست و تطبیق دستور «دقیق» است، پس هیچ
``startswith`` عمومی‌ای نمی‌تواند این سه دستور را با چیز دیگری اشتباه
بگیرد.
"""
try:
    from splusthon.tl.types import MessageEntityBlockquote, MessageEntityBold
except ImportError:
    class MessageEntityBlockquote:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length
    class MessageEntityBold:
        def __init__(self, offset=0, length=0):
            self.offset = offset
            self.length = length

from modules.group_expiry import (
    EXPIRED_MESSAGE,
    STATUS_COMMAND,
    all_records,
    build_confirmation,
    build_expired_message,
    build_status_message,
    clear_expiry,
    due_groups,
    expires_at,
    has_expiry,
    is_expired,
    MAX_NOTICE_ATTEMPTS,
    mark_notified,
    match_command,
    match_status_command,
    record_notice_attempt,
    refresh_caches,
    remaining_text,
    set_expiry,
    title_of,
    update_title,
)
from modules.owner_check import is_global_owner
# دسترسی از طریق خودِ ماژول (نه نام مستقیم) تا در تست بتوان تابع را
# monkeypatch کرد و در زمان اجرا هم همیشه آخرین وضعیت storage خوانده شود.
from modules import admin_storage, group_storage

# پیامی که وقتی گروه منقضی است به غیرمالک نشان داده می‌شود.
EXPIRED_NOTICE = (
    "⛔ مدت زمان فعال بودن این گروه به پایان رسیده است.\n\n"
    "برای فعال‌سازی دوباره، مالک اصلی باید یکی از دستورهای "
    "«۵ روز»، «یک هفته»، «دو هفته» یا «یک ماه» را ارسال کند."
)

CHECK_INTERVAL_SECONDS = 20

# ⏳ همگام‌سازی «لیست انقضا» — فقط برای تازه نگه‌داشتن خودِ لیست.
# انقضای واقعی گروه هرگز منتظر این حلقه نمی‌ماند: ناظرِ انقضا
# (CHECK_INTERVAL_SECONDS) و گیتِ is_expired در مسیر پیام، سرِ زمانِ
# واقعی ربات را در گروه خاموش می‌کنند.
EXPIRY_LIST_SYNC_SECONDS = 24 * 60 * 60

# عنوانِ دیده‌شدهٔ هر گروه؛ تا وقتی نام عوض نشده هیچ مقایسه/نوشتنی
# روی storage انجام نمی‌شود (مسیر داغ پیام ارزان می‌ماند).
_title_memo = {}
# گروه‌هایی که یک‌بار برایشان RPC گرفتن نام انجام شده؛ تا این کار برای
# هر پیام تکرار نشود (حداکثر یک RPC به ازای هر گروه در هر اجرا).
_title_probed = set()


def _entities(spans):
    """تبدیل span های خنثی به entity واقعی splusthon."""
    built = []
    for kind, offset, length in spans:
        if kind == "blockquote":
            built.append(MessageEntityBlockquote(offset=offset, length=length))
        elif kind == "bold":
            built.append(MessageEntityBold(offset=offset, length=length))
    return built


def _log(logger, message):
    if logger is not None:
        try:
            logger.log_info(message)
        except Exception:
            pass


def _log_error(logger, message):
    if logger is not None:
        try:
            logger.log_error(message)
        except Exception:
            pass


async def handle(bot, event, chat_id, sender, text, logger=None):
    """اگر پیام یکی از سه دستور انقضا باشد آن را پردازش می‌کند.

    ``True`` یعنی پیام مصرف شد و هندلر اصلی نباید ادامه دهد.
    """
    command = match_command(text)
    if command is None:
        return False

    # فقط مالک اصلی. برای بقیه هیچ پاسخی داده نمی‌شود تا این دستورها
    # برای کاربران عادی اصلاً وجود نداشته باشند.
    if not is_global_owner(sender):
        _log(logger, "GROUP EXPIRY DENIED "
                     f"chat_id={chat_id} user_id={getattr(sender, 'id', None)} "
                     f"command={command!r} reason=not_global_owner")
        return True

    title = getattr(await _safe_chat(event), "title", "") or ""
    result = set_expiry(chat_id, command, title=title)
    if result is None:
        _log_error(logger, f"GROUP EXPIRY SET FAILED chat_id={chat_id} "
                           f"command={command!r}")
        return True

    text_out, spans = build_confirmation(
        result["activated_at"], result["expires_at"])
    await event.reply(text_out, formatting_entities=_entities(spans))
    _log(logger, "GROUP EXPIRY SET "
                 f"chat_id={chat_id} command={command!r} days={result['days']} "
                 f"activated_at={result['activated_at'].isoformat()} "
                 f"expires_at={result['expires_at'].isoformat()}")
    return True


async def _safe_chat(event):
    try:
        return await event.get_chat()
    except Exception:
        return None


def blocks_message(chat_id, sender):
    """آیا این پیام باید به دلیل انقضای گروه متوقف شود.

    مالک اصلی همیشه عبور می‌کند تا بتواند گروه را دوباره فعال کند.
    """
    if not is_expired(chat_id):
        return False
    return not is_global_owner(sender)


# ---------------------------------------------------------------------------
# ⏳ دستور «مهلت گروه»
# ---------------------------------------------------------------------------
def has_status_permission(chat_id, sender):
    """مالک اصلی ربات، مالک گروه، یا ادمینِ ثبت‌شدهٔ همان گروه."""
    if sender is None:
        return False
    user_id = getattr(sender, "id", sender)
    if user_id is None:
        return False
    if is_global_owner(user_id):
        return True
    try:
        owner = group_storage.get_group_owner(chat_id)
    except Exception:
        owner = None
    if owner is not None and str(user_id) == str(owner):
        return True
    try:
        return bool(admin_storage.is_admin(
            chat_id, user_id, getattr(sender, "username", None)))
    except Exception:
        return False


def sync_group_title(chat_id, title, logger=None):
    """🔄 نام فعلی گروه را در هر دو storage نگه می‌دارد.

    با اولین پیامِ بعد از تغییر نام، عنوان جدید ذخیره می‌شود تا دستور
    «مهلت گروه» و «لیست انقضا» همیشه نام واقعی را نشان دهند. شناسهٔ
    گروه هرگز تغییر نمی‌کند، پس تغییر نام نه گروه جدید می‌سازد و نه
    اشتراک را از بین می‌برد.

    ارزان است: تا وقتی عنوان مثل دفعهٔ قبل باشد هیچ خواندن/نوشتنی روی
    فایل انجام نمی‌شود.
    """
    title = str(title or "").strip()
    if not title or chat_id is None:
        return False
    if _title_memo.get(chat_id) == title:
        return False
    _title_memo[chat_id] = title

    changed = False
    try:
        if group_storage.update_group_title(chat_id, title):
            changed = True
    except Exception as error:
        _log_error(logger, f"GROUP EXPIRY TITLE SYNC STORAGE FAILED "
                           f"chat_id={chat_id} error={error!r}")
    try:
        if update_title(chat_id, title):
            changed = True
    except Exception as error:
        _log_error(logger, f"GROUP EXPIRY TITLE SYNC RECORD FAILED "
                           f"chat_id={chat_id} error={error!r}")
    if changed:
        _log(logger, "GROUP EXPIRY TITLE UPDATED "
                     f"chat_id={chat_id} title={title[:60]!r}")
    return changed


def _stored_title(chat_id):
    """عنوان ذخیره‌شده در groups.json (بدون هیچ RPC)."""
    try:
        groups = group_storage.load_groups()
    except Exception:
        return ""
    if not isinstance(groups, dict):
        return ""
    key = str(group_storage.normalize_group_id(chat_id))
    record = groups.get(key)
    if record is None:
        record = groups.get(str(chat_id))
    if not isinstance(record, dict):
        return ""
    return str(record.get("title") or "").strip()


async def _live_title(event, chat_id):
    """نام فعلی گروه؛ اول از کش خود رویداد، و فقط در نبودِ آن با RPC."""
    chat = getattr(event, "chat", None)
    title = getattr(chat, "title", None)
    if title:
        return str(title).strip()
    try:
        chat = await event.get_chat()
    except Exception:
        chat = None
    title = getattr(chat, "title", None)
    return str(title).strip() if title else ""


async def sync_title_from_event(event, chat_id, logger=None):
    """🔄 نام گروه را با اولین پیامِ بعد از تغییر نام به‌روز می‌کند.

    مسیر ارزان: عنوان از کشِ خود رویداد خوانده می‌شود و هیچ RPC ای
    انجام نمی‌شود. فقط اگر رویداد عنوان نداشت و تا حالا هم برای این
    گروه نامی نگرفته باشیم، یک‌بار ``get_chat`` صدا زده می‌شود؛ پس به
    ازای هر گروه در هر اجرای ربات حداکثر یک RPC اضافه می‌شود و مسیر
    داغِ پیام کند نمی‌شود.
    """
    title = getattr(getattr(event, "chat", None), "title", None)
    if not title and chat_id is not None and chat_id not in _title_probed:
        _title_probed.add(chat_id)
        title = await _live_title(event, chat_id)
    if not title:
        return False
    return sync_group_title(chat_id, title, logger)


async def handle_status(bot, event, chat_id, sender, text, logger=None,
                        allow=None):
    """دستور «مهلت گروه» — مهلت واقعیِ باقی‌ماندهٔ همان گروه.

    ``True`` یعنی پیام مصرف شد. دسترسی: مالک اصلی، مالک گروه یا ادمین
    ثبت‌شدهٔ همان گروه؛ برای بقیه هیچ پاسخی داده نمی‌شود.
    """
    if not match_status_command(text):
        return False

    user_id = getattr(sender, "id", sender)
    allowed = has_status_permission(chat_id, sender) if allow is None else bool(allow)
    if not allowed:
        _log(logger, "GROUP EXPIRY STATUS DENIED "
                     f"chat_id={chat_id} user_id={user_id} "
                     f"command={STATUS_COMMAND!r} reason=not_admin_or_owner")
        return True

    title = await _live_title(event, chat_id)
    if title:
        sync_group_title(chat_id, title, logger)
    else:
        title = _stored_title(chat_id) or title_of(chat_id) or ""

    remaining = remaining_text(chat_id)
    text_out, spans = build_status_message(title, remaining)
    await event.reply(text_out, formatting_entities=_entities(spans))
    _log(logger, "GROUP EXPIRY STATUS SENT "
                 f"chat_id={chat_id} user_id={user_id} "
                 f"title={str(title)[:60]!r} remaining={remaining!r} "
                 f"expires_at={_record_expiry(chat_id)}")
    return True


def _record_expiry(chat_id):
    """زمان انقضای رکورد برای لاگ (None اگر رکوردی نباشد)."""
    try:
        moment = expires_at(chat_id)
        return moment.isoformat() if moment else None
    except Exception:
        return None


async def run_expiry_watcher(bot, deactivate, interval=None,
                             logger=None, iterations=None):
    """حلقهٔ پس‌زمینه: گروه‌های منقضی را بدون نیاز به هیچ پیامی می‌بندد.

    ``deactivate(chat_id, title)`` توسط فراخوان داده می‌شود تا این ماژول
    به storage گروه‌ها وابسته نشود.
    """
    import asyncio

    delay = CHECK_INTERVAL_SECONDS if interval is None else interval
    rounds = 0
    while True:
        try:
            await check_once(bot, deactivate, logger=logger)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_error(logger, f"GROUP EXPIRY WATCHER FAILED error={error!r}")
        rounds += 1
        if iterations is not None and rounds >= iterations:
            return rounds
        await asyncio.sleep(delay)


async def check_once(bot, deactivate, logger=None):
    """یک بار بررسی می‌کند و هر گروه منقضی را می‌بندد.

    تعداد گروه‌های بسته‌شده را برمی‌گرداند.
    """
    closed = 0
    due = due_groups()
    _log(logger, f"EXPIRY CHECK due_count={len(due)}")
    for key, record in due:
        title = record.get("title", "") or ""
        _log(logger, "EXPIRY FOUND "
                     f"group_id={key} expires_at={record.get('expires_at')} "
                     f"title={title!r}")
        try:
            chat_id = int(key)
        except (TypeError, ValueError):
            chat_id = key

        _log(logger, "EXPIRY ACTION START "
                     f"group_id={chat_id} title={title!r}")
        try:
            deactivate(chat_id, title)
            _log(logger, f"GROUP EXPIRY DEACTIVATED chat_id={chat_id} "
                         f"title={title!r} expires_at={record.get('expires_at')}")
        except Exception as error:
            _log_error(logger, f"GROUP EXPIRY DEACTIVATE FAILED "
                               f"chat_id={chat_id} error={error!r}")
            continue

        message, spans = build_expired_message()
        # ⚠️ غیرفعال‌سازی بالا انجام شد؛ از اینجا به بعد فقط «اعلام» است.
        # اگر ارسال نشود، گروه همچنان خاموش می‌ماند و کارِ ربات در آن
        # تمام است؛ پس تلاش‌ها کران دارند و بی‌پایان تکرار نمی‌شوند.
        delivered = False
        target_gone = False
        for target in _targets(chat_id, key):
            try:
                sent = await bot.client.send_message(
                    target, message, formatting_entities=_entities(spans))
                cleanup = getattr(bot, "notice_cleanup", None)
                if cleanup is not None:
                    cleanup.schedule(target, sent)
                delivered = True
                break
            except Exception as error:
                _log_error(logger, f"GROUP EXPIRY NOTICE FAILED "
                                   f"chat_id={target} error={error!r}")
                if _is_target_gone(error):
                    target_gone = True
                    break

        if delivered:
            if not mark_notified(key):
                _log_error(logger, "EXPIRY NOTIFICATION STATE FAILED "
                                   f"group_id={key}")
            else:
                _log(logger, f"EXPIRY NOTIFICATION SENT group_id={key} "
                             f"chat_id={target}")
                _log(logger, f"GROUP EXPIRY NOTICE SENT chat_id={target}")
        elif target_gone:
            mark_notified(key)
            _log_error(logger, "EXPIRY TARGET INVALID REMOVED "
                               f"chat_id={chat_id}")
        else:
            attempts = record_notice_attempt(key)
            if attempts >= MAX_NOTICE_ATTEMPTS:
                mark_notified(key)
                _log_error(logger, "EXPIRY NOTICE GIVE UP "
                                   f"group_id={key} attempts={attempts} "
                                   f"limit={MAX_NOTICE_ATTEMPTS} "
                                   "reason=notice_not_delivered")
            else:
                _log(logger, "EXPIRY NOTICE WILL RETRY "
                             f"group_id={key} attempts={attempts} "
                             f"limit={MAX_NOTICE_ATTEMPTS}")
        closed += 1
    return closed


def _is_target_gone(error):
    """آیا خودِ گروه/شناسه وجود ندارد (پس تلاش دوباره بی‌فایده است)."""
    text = f"{error!r}".upper()
    return "404" in text or "NOT_FOUND" in text


def _targets(chat_id, key):
    """شکل‌های ممکن شناسه برای ارسال پیام به گروه."""
    targets = []
    try:
        short = int(key)
    except (TypeError, ValueError):
        return [chat_id]
    for candidate in (chat_id, -short, -(1_000_000_000_000 + short), short):
        if candidate not in targets:
            targets.append(candidate)
    return targets


# ---------------------------------------------------------------------------
# 🔄 همگام‌سازی دوره‌ای «لیست انقضا»
# ---------------------------------------------------------------------------
def _refresh_stores():
    """کش هر دو storage را بی‌اعتبار می‌کند تا خواندن بعدی از فایل باشد."""
    try:
        refresh_caches()
    except Exception:
        pass
    group_storage._cache = None
    group_storage._cache_mtime = None


def sync_expiry_list(logger=None):
    """🔄 «لیست انقضا» را با وضعیت واقعی گروه‌ها یکی می‌کند.

    ⚠️ این همگام‌سازی فقط برای خودِ لیست است. منقضی شدن واقعی گروه هیچ
    ارتباطی با آن ندارد: ناظرِ انقضا هر ``CHECK_INTERVAL_SECONDS`` گروه را
    می‌بندد و گیتِ ``is_expired`` در مسیر پیام، دقیقاً سرِ لحظهٔ انقضا
    جلوی هر دستور عادی را می‌گیرد.

    کارها:
      ۱. کش هر دو storage دور ریخته و وضعیت از فایل خوانده می‌شود.
      ۲. رکوردِ انقضای گروهی که دیگر در ``groups.json`` ثبت نیست حذف
         می‌شود (گروه‌های منقضی‌شده‌ای که در لیست جا می‌ماندند).
      ۳. عنوان هر رکورد با نام فعلی گروه یکی می‌شود.
      ۴. وضعیت و زمان باقی‌مانده از تاریخ واقعیِ همان لحظه محاسبه
         می‌شود؛ گروه تمدیدشده دیگر «منقضی» نشان داده نمی‌شود.

    خروجی یک dict خلاصه برای لاگ است.
    """
    _refresh_stores()
    summary = {
        "groups": 0,
        "with_expiry": 0,
        "expired": 0,
        "active": 0,
        "orphans_removed": 0,
        "titles_updated": 0,
        "skipped": "",
    }
    try:
        groups = group_storage.load_groups()
        groups = groups if isinstance(groups, dict) else {}
    except Exception as error:
        _log_error(logger, f"EXPIRY SYNC GROUPS LOAD FAILED error={error!r}")
        groups = {}

    # اگر groups.json خالی/خراب است هیچ رکوردی حذف نمی‌شود؛ وگرنه یک
    # فایل موقتاً ناخوانا می‌توانست کل اشتراک‌ها را پاک کند.
    if not groups:
        summary["skipped"] = "no_registered_groups"
        _log_error(logger, "EXPIRY SYNC SKIPPED reason=no_registered_groups "
                           "(هیچ رکورد انقضایی حذف نشد)")
        return summary

    registered = set()
    for group_id in groups:
        registered.add(str(group_id))
        try:
            registered.add(str(group_storage.normalize_group_id(group_id)))
        except Exception:
            pass

    records = all_records()
    for key in records:
        if str(key) in registered:
            continue
        try:
            if clear_expiry(key):
                summary["orphans_removed"] += 1
                _log(logger, "EXPIRY SYNC ORPHAN REMOVED "
                             f"group_id={key!r} reason=not_registered")
        except Exception as error:
            _log_error(logger, f"EXPIRY SYNC ORPHAN REMOVE FAILED "
                               f"group_id={key!r} error={error!r}")

    summary["groups"] = len(groups)
    for group_id, group in groups.items():
        group = group if isinstance(group, dict) else {}
        if not has_expiry(group_id):
            continue
        summary["with_expiry"] += 1
        title = str(group.get("title") or "").strip()
        if title:
            try:
                if update_title(group_id, title):
                    summary["titles_updated"] += 1
                    _log(logger, "EXPIRY SYNC TITLE UPDATED "
                                 f"group_id={group_id} title={title[:60]!r}")
            except Exception as error:
                _log_error(logger, f"EXPIRY SYNC TITLE UPDATE FAILED "
                                   f"group_id={group_id} error={error!r}")
        if is_expired(group_id):
            summary["expired"] += 1
        else:
            summary["active"] += 1

    _log(logger, "EXPIRY LIST SYNC "
                 f"groups={summary['groups']} with_expiry={summary['with_expiry']} "
                 f"active={summary['active']} expired={summary['expired']} "
                 f"orphans_removed={summary['orphans_removed']} "
                 f"titles_updated={summary['titles_updated']}")
    return summary


async def run_expiry_list_sync(interval=None, logger=None, iterations=None):
    """حلقهٔ پس‌زمینهٔ همگام‌سازی لیست انقضا (پیش‌فرض: هر ۲۴ ساعت).

    مثل ناظرِ انقضا هیچ‌وقت با یک خطا نمی‌میرد. ``iterations`` فقط برای
    تست است.
    """
    import asyncio

    delay = EXPIRY_LIST_SYNC_SECONDS if interval is None else interval
    rounds = 0
    while True:
        try:
            sync_expiry_list(logger)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_error(logger, f"EXPIRY LIST SYNC LOOP FAILED error={error!r}")
        rounds += 1
        if iterations is not None and rounds >= iterations:
            return rounds
        await asyncio.sleep(delay)
