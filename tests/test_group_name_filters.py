"""Regression tests for group-local custom advertising-name filters."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from handlers import message_handler as mh
from modules import ad_name_detector
from modules.group_dispatch import LANE_ADMIN, PRIORITY_ADMIN, classify_priority


CHAT_A = -100900123
CHAT_B = -100900124
USER_ID = 88001


def user(name="علی", *, username=None, user_id=USER_ID):
    return SimpleNamespace(
        id=user_id,
        first_name=name,
        last_name=None,
        username=username,
    )


class ReplyEvent:
    def __init__(self):
        self.replies = []

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return None


class Logger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(message)

    def log_error(self, message):
        self.errors.append(message)


class ModerationQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, chat_id, action, operation, **kwargs):
        self.jobs.append({
            "chat_id": chat_id,
            "action": action,
            "operation": operation,
            **kwargs,
        })
        return True


class EnforcementBot:
    def __init__(self):
        self.logger = Logger()
        self.moderation_queue = ModerationQueue()
        self.punished_users = set()


class GroupNameFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.filter_path = Path(self.temp_dir.name) / "ad_name_filters.json"
        ad_name_detector.use_filter_file(self.filter_path)

    def tearDown(self):
        ad_name_detector.use_filter_file(None)
        self.temp_dir.cleanup()

    def test_text_filters_are_group_local_and_persistent(self):
        self.assertEqual(ad_name_detector.add_group_filter(CHAT_A, "حسین"),
                         ("added", "حسین"))
        self.assertEqual(ad_name_detector.add_group_filter(CHAT_B, "رضا"),
                         ("added", "رضا"))
        self.assertTrue(self.filter_path.exists())
        persisted = json.loads(self.filter_path.read_text(encoding="utf-8"))
        self.assertEqual(len(persisted), 2)

        # Re-read from disk, rather than relying on the in-memory cache.
        ad_name_detector.reset_filter_cache()
        self.assertEqual(ad_name_detector.list_group_filters(CHAT_A), ["حسین"])
        self.assertEqual(ad_name_detector.list_group_filters(CHAT_B), ["رضا"])
        self.assertIsNotNone(ad_name_detector.custom_filter_reason(CHAT_A, user("حسین احمدی")))
        self.assertIsNone(ad_name_detector.custom_filter_reason(CHAT_B, user("حسین احمدی")))
        # Custom terms intentionally inspect display names only, never usernames.
        self.assertIsNone(ad_name_detector.custom_filter_reason(
            CHAT_A, user("علی", username="حسین")
        ))

    def test_emoji_filter_matches_display_name_and_alias_removes_only_its_group(self):
        self.assertEqual(ad_name_detector.add_group_filter(CHAT_A, "🍆"),
                         ("added", "🍆"))
        self.assertIsNotNone(ad_name_detector.custom_filter_reason(CHAT_A, user("علی🍆")))
        self.assertIsNone(ad_name_detector.custom_filter_reason(CHAT_B, user("علی🍆")))

        event = ReplyEvent()
        handled = asyncio.run(ad_name_detector.handle_filter_command(
            event, CHAT_A, "لغو اسم 🍆", authorized=True,
        ))
        self.assertTrue(handled)
        self.assertEqual(event.replies[0][0], "نام : 🍆 از فیلتر خارج شد")
        self.assertEqual(ad_name_detector.list_group_filters(CHAT_A), [])

    def test_add_remove_notices_are_full_bold_blockquotes_and_term_is_supplied(self):
        event = ReplyEvent()
        self.assertTrue(asyncio.run(ad_name_detector.handle_filter_command(
            event, CHAT_A, "فیلتر اسم حسین", authorized=True,
        )))
        added_text, added_kwargs = event.replies[-1]
        self.assertEqual(added_text, "نام : حسین فیلتر شد")
        self._assert_full_bold_blockquote(added_text, added_kwargs["formatting_entities"])

        self.assertTrue(asyncio.run(ad_name_detector.handle_filter_command(
            event, CHAT_A, "لیست فیلتر اسم", authorized=True,
        )))
        self.assertEqual(event.replies[-1][0], "📋 فیلترهای اسم همین گروه:\n\nحسین")

        # Diacritics normalize to the same stored term, but the response must
        # still show the term that the registered admin supplied in this command.
        self.assertTrue(asyncio.run(ad_name_detector.handle_filter_command(
            event, CHAT_A, "حذف فیلتر اسم حُسین", authorized=True,
        )))
        removed_text, removed_kwargs = event.replies[-1]
        self.assertEqual(removed_text, "نام : حُسین از فیلتر خارج شد")
        self._assert_full_bold_blockquote(removed_text, removed_kwargs["formatting_entities"])

    def test_only_registered_group_owner_or_admin_has_command_permission(self):
        with (
            patch.object(mh, "get_group_owner", return_value=101),
            patch.object(mh, "is_admin", return_value=False),
        ):
            self.assertTrue(mh._has_registered_name_filter_permission(CHAT_A, 101, "owner"))
            self.assertFalse(mh._has_registered_name_filter_permission(CHAT_A, 202, "other"))
        with (
            patch.object(mh, "get_group_owner", return_value=101),
            patch.object(mh, "is_admin", return_value=True),
        ):
            self.assertTrue(mh._has_registered_name_filter_permission(CHAT_A, 202, "admin"))

    def test_permission_is_required_for_add_list_and_remove(self):
        for command in ("فیلتر اسم حسین", "لیست فیلتر اسم", "لغو اسم حسین"):
            with self.subTest(command=command):
                event = ReplyEvent()
                self.assertTrue(asyncio.run(ad_name_detector.handle_filter_command(
                    event, CHAT_A, command, authorized=False,
                )))
                self.assertEqual(event.replies[-1][0], ad_name_detector.PERMISSION_DENIED)
        self.assertEqual(ad_name_detector.list_group_filters(CHAT_A), [])

        private_event = ReplyEvent()
        self.assertTrue(asyncio.run(ad_name_detector.handle_filter_command(
            private_event, CHAT_A, "فیلتر اسم حسین", authorized=True,
            is_private=True,
        )))
        self.assertEqual(private_event.replies[-1][0], ad_name_detector.PRIVATE_ONLY)
        self.assertEqual(ad_name_detector.list_group_filters(CHAT_A), [])

    def test_first_message_uses_existing_enforcement_and_keeps_owner_admin_exemptions(self):
        ad_name_detector.add_group_filter(CHAT_A, "حسین")

        with (
            patch.object(mh, "is_global_owner", return_value=False),
            patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
            patch.object(mh.punishment_mode, "is_mute", return_value=False),
        ):
            bot = EnforcementBot()
            handled = mh.enforce_advertising_name(
                bot, CHAT_A, user("حسین"), current_message_id=41,
                source="message",
            )
        self.assertTrue(handled)
        self.assertEqual(len(bot.moderation_queue.jobs), 1)
        self.assertEqual(bot.moderation_queue.jobs[0]["chat_id"], CHAT_A)
        self.assertTrue(any(
            "reason='فیلتر اسم (حسین)'" in line for line in bot.logger.infos
        ))

        # A matching name in another group has no action at all.
        other_bot = EnforcementBot()
        with (
            patch.object(mh, "is_global_owner", return_value=False),
            patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
        ):
            self.assertFalse(mh.enforce_advertising_name(other_bot, CHAT_B, user("حسین")))
        self.assertEqual(other_bot.moderation_queue.jobs, [])

        for global_owner, registered_admin in ((True, False), (False, True)):
            with self.subTest(global_owner=global_owner, registered_admin=registered_admin):
                exempt_bot = EnforcementBot()
                with (
                    patch.object(mh, "is_global_owner", return_value=global_owner),
                    patch.object(
                        mh.admin_tools, "has_admin_permission",
                        return_value=registered_admin,
                    ),
                ):
                    self.assertFalse(mh.enforce_advertising_name(
                        exempt_bot, CHAT_A, user("حسین")
                    ))
                self.assertEqual(exempt_bot.moderation_queue.jobs, [])

    def test_parser_and_dispatch_mark_every_management_syntax_administrative(self):
        self.assertEqual(ad_name_detector.parse_filter_command("لیست فیلتر اسم"), ("list", None))
        self.assertEqual(ad_name_detector.parse_filter_command("فیلتر اسم حسین"), ("add", "حسین"))
        self.assertEqual(ad_name_detector.parse_filter_command("حذف فیلتر اسم حسین"), ("remove", "حسین"))
        self.assertEqual(ad_name_detector.parse_filter_command("لغو اسم 🍆"), ("remove", "🍆"))
        for command in (
            "لیست فیلتر اسم", "فیلتر اسم حسین",
            "حذف فیلتر اسم حسین", "لغو اسم 🍆",
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    classify_priority(command), (PRIORITY_ADMIN, LANE_ADMIN)
                )

    def _assert_full_bold_blockquote(self, text, entities):
        self.assertEqual(len(entities), 2)
        expected_length = len(text.encode("utf-16-le")) // 2
        self.assertEqual({entity.__class__.__name__ for entity in entities}, {
            "MessageEntityBold", "MessageEntityBlockquote",
        })
        self.assertTrue(all(
            entity.offset == 0 and entity.length == expected_length
            for entity in entities
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
