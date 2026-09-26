"""📋 گزارش فقط-خواندنی انقضای گروه‌ها.

این ماژول هیچ فایل یا state جدیدی نمی‌سازد. اطلاعات گروه‌ها را از
``modules.group_storage`` و مهلت‌ها را از ``modules.group_expiry`` می‌خواند.
هر دو منبع cache وابسته به mtime دارند، پس هر فراخوان گزارش آخرین تغییر
فایل‌ها را می‌بیند.
"""
from datetime import datetime, timezone

from modules import group_expiry, group_storage

_HEADER = "📋 لیست انقضای گروه‌ها"
_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def _log_error(logger, message):
    if logger is None:
        return
    try:
        logger.log_error(message)
    except Exception:
        pass


def _digits(value):
    return str(value).translate(_PERSIAN_DIGITS)


def _remaining_text(expires_at, now):
    """Format an aware UTC expiry moment without timezone-dependent rounding.

    تنها پیاده‌سازی محاسبهٔ باقی‌مانده در ``modules.group_expiry`` است؛
    این تابع فقط همان را صدا می‌زند تا گزارش و دستور «مهلت گروه» هرگز
    عدد متفاوتی نشان ندهند.
    """
    return group_expiry.format_remaining(expires_at, now)


def _refresh_sources():
    """کش هر دو storage را بی‌اعتبار می‌کند تا گزارش از خودِ فایل بخواند.

    بدون این کار، گزارشی که در یک پروسِ بلندمدت ساخته می‌شود می‌توانست
    وضعیت قدیمی را نشان دهد؛ حالا هر «لیست انقضا» وضعیت لحظه‌ای است.
    """
    try:
        group_expiry.refresh_caches()
    except Exception:
        pass
    try:
        group_storage._cache = None
        group_storage._cache_mtime = None
    except Exception:
        pass


def _sources(logger):
    try:
        groups = group_storage.load_groups()
        groups = groups if isinstance(groups, dict) else {}
    except Exception as error:
        _log_error(logger, f"EXPIRY REPORT GROUP LOAD FAILED error={error!r}")
        groups = {}
    try:
        expiry_records = group_expiry.all_records()
    except Exception as error:
        _log_error(logger, f"EXPIRY REPORT LOAD FAILED error={error!r}")
        expiry_records = {}
    return groups, expiry_records


def _moment(now):
    value = now or datetime.now(timezone.utc)
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
            else value.astimezone(timezone.utc))


def build_report(logger=None, now=None):
    """Build the detailed legacy expiry report (used by the existing private route).

    گزارش همیشه از وضعیتِ لحظه‌ای ساخته می‌شود: کش هر دو storage قبل از
    ساخت بی‌اعتبار می‌شود، پس گروه تمدیدشده با تاریخ جدید و گروه
    منقضی‌شده با وضعیت واقعی دیده می‌شود — نه با مقدار قدیمی.
    """
    moment = _moment(now)
    _refresh_sources()
    groups, expiry_records = _sources(logger)
    if not groups:
        return _HEADER + "\n\nℹ️ هیچ گروه ثبت‌شده‌ای وجود ندارد."

    rows = [_HEADER]
    expired_count = 0
    for index, (group_id, group) in enumerate(groups.items(), 1):
        group = group if isinstance(group, dict) else {}
        record = group_expiry.get_record(group_id)
        title = (
            str(group.get("title") or "").strip()
            or str((record or {}).get("title") or "").strip()
            or "گروه بدون نام"
        )
        expired = bool(record) and group_expiry.is_expired(group_id, now=moment)
        if expired:
            expired_count += 1
        prefix = "❌" if expired else f"{_digits(index)}️⃣"
        lines = [f"{prefix} گروه: {title}", f"🆔 شناسه: {group_id}"]
        if not record:
            lines.append("⏳ وضعیت: تاریخ انقضا ثبت نشده")
        else:
            expires = group_expiry.expires_at(group_id)
            if expires is None:
                _log_error(logger, "EXPIRY REPORT INVALID RECORD "
                           f"group_id={group_id!r} record={record!r}")
                lines.append("⏳ وضعیت: تاریخ انقضا نامعتبر است")
            else:
                remaining = _remaining_text(expires, moment)
                lines.append("⏳ وضعیت: منقضی شده" if remaining is None
                             else f"⏳ باقی‌مانده: {remaining}")
                # تاریخ واقعیِ همان رکورد؛ پس گروه تمدیدشده دیگر با تاریخ
                # منقضی‌شدهٔ قبلی نمایش داده نمی‌شود.
                lines.append(
                    f"📅 تاریخ انقضا: {group_expiry.format_datetime(expires)}")
        # وضعیت واقعی ربات در گروه، تا گروهِ بسته‌شده «فعال» به نظر نرسد.
        bot_active = bool(group.get("active")) and not expired
        lines.append(
            "🔌 ربات در گروه: فعال" if bot_active else "🔌 ربات در گروه: خاموش")
        rows.append("\n".join(lines))

    registered_keys = {str(key) for key in groups}
    orphan_count = 0
    for expiry_key in expiry_records:
        if str(expiry_key) not in registered_keys:
            orphan_count += 1
            _log_error(logger, "EXPIRY REPORT ORPHAN RECORD "
                       f"group_id={expiry_key!r} reason=not_in_groups_storage")
    rows.append(
        f"🔄 همگام‌سازی: {_digits(len(groups))} گروه | "
        f"{_digits(expired_count)} منقضی | "
        f"{_digits(orphan_count)} رکورد بدون گروه"
        + (f"\n🕒 زمان گزارش: {group_expiry.format_datetime(moment)}")
    )
    return "\n\n".join(rows)


def build_group_list(logger=None, now=None):
    """Build the compact owner-in-group list: group name and remaining time only."""
    moment = _moment(now)
    groups, _expiry_records = _sources(logger)
    if not groups:
        return _HEADER + "\n\nℹ️ هیچ گروه ثبت‌شده‌ای وجود ندارد."

    rows = [_HEADER]
    listed = 0
    for group_id, group in groups.items():
        record = group_expiry.get_record(group_id)
        if not record:
            continue
        expires = group_expiry.expires_at(group_id)
        if expires is None:
            _log_error(logger, "EXPIRY LIST INVALID RECORD "
                       f"group_id={group_id!r} record={record!r}")
            continue
        group = group if isinstance(group, dict) else {}
        title = (str(group.get("title") or "").strip()
                 or str(record.get("title") or "").strip()
                 or "گروه بدون نام")
        listed += 1
        remaining = _remaining_text(expires, moment)
        status = "منقضی شده" if remaining is None else f"{remaining} باقی مانده"
        rows.append(f"{listed}. {title}\n⏳ {status}")

    if not listed:
        return _HEADER + "\n\nℹ️ هیچ تاریخ انقضایی ثبت نشده است."
    return "\n\n".join(rows)
