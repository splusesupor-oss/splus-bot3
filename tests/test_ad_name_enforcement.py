"""Regression tests for robust advertising-name detection and enforcement."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from handlers import message_handler as mh
from modules import ad_name_detector, message_tracker
from modules.spam_history import clear_user, save_history_message


CHAT = -100900123
USER_ID = 88001


def user(name="علی", *, username=None, last_name=None, user_id=USER_ID):
    return SimpleNamespace(
        id=user_id,
        first_name=name,
        last_name=last_name,
        username=username,
    )


class Logger:
    def __init__(self):
        self.infos = []
        self.errors = []

    def log_info(self, message):
        self.infos.append(message)

    def log_error(self, message):
        self.errors.append(message)


class DeleteQueue:
    def __init__(self, order):
        self.order = order
        self.calls = []

    def enqueue(self, chat_id, message_ids, **kwargs):
        ids = list(message_ids)
        self.calls.append((chat_id, ids, kwargs))
        self.order.append(("delete", ids))
        future = asyncio.get_running_loop().create_future()
        future.set_result((len(ids), []))
        return future


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


class AdminActions:
    def __init__(self, order):
        self.order = order
        self.calls = []

    async def ban_user(self, chat_id, user_id, **kwargs):
        self.order.append(("punish", user_id))
        self.calls.append((chat_id, user_id, kwargs))
        return True


class Client:
    def __init__(self, history=()):
        self.sent = []
        self.history = list(history)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(id=999)

    async def iter_messages(self, chat_id, **kwargs):
        for message in self.history:
            yield message


class Bot:
    def __init__(self, history=()):
        self.order = []
        self.logger = Logger()
        self.client = Client(history)
        self.message_delete_queue = DeleteQueue(self.order)
        self.moderation_queue = ModerationQueue()
        self.admin_actions = AdminActions(self.order)
        self.punished_users = set()
        self.outgoing_sender = None


class AdvertisingNameDetectorTests(unittest.TestCase):
    def assert_ad(self, value, *, username=False):
        target = user("علی", username=value) if username else user(value)
        self.assertIsNotNone(ad_name_detector.reason(target), value)

    def test_exact_required_words(self):
        for value in (
            "سکس", "بیوگرافی", "فیلم", "زوری", "کانال", "گروه",
            "یکی بیاد", "خانوم", "پسر", "دختر",
        ):
            with self.subTest(value=value):
                self.assert_ad(value)
        self.assert_ad("کانال", username=True)

    def test_spacing_half_space_and_broken_forms(self):
        for value in (
            "س ک س", "س\u200cک\u200cس", "س.ک-س", "س\u200b/ک\u200d_س",
            "ب ی و گ ر ا ف ی", "ب\u200cی.و-گ_ر/ا|ف+ی",
            "ی ک ی---ب ی ا د", "ف ی ل م", "د خ ت ر",
        ):
            with self.subTest(value=value):
                self.assert_ad(value)

    def test_tatweel_and_repeated_letters(self):
        for value in (
            "ســــکــــس", "سسسسککککسسس", "س س س ک ک س س",
            "بــــیــــوگــــرافــــی", "بییییوگرااافی",
            "ففیییلممم", "خخااننوومم", "پپسسرر",
        ):
            with self.subTest(value=value):
                self.assert_ad(value)

    def test_unicode_equivalent_letters(self):
        for value in (
            "س ك س", "ب ي و گ ر ا ف ى", "ك ا ن ا ل", "ﮐﺎﻧﺎﻝ",
        ):
            with self.subTest(value=value):
                self.assert_ad(value)

    def test_required_emojis_and_combined_name(self):
        for value in ("💦", "🌈", "👄", "💋", "🤤", "😰", "🥵", "🍑"):
            with self.subTest(value=value):
                self.assert_ad(value)
        self.assert_ad("خانوم 💋 ی ک ی بیاد کانال")

    def test_legitimate_names_and_words_are_not_false_positives(self):
        for value in (
            "علی رضایی", "محمد حسین", "سارا احمدی", "کسری", "پیروز",
            "گروهان", "فیلمبردار", "دخترانه", "پسرانه", "کانالیزه",
            "خانومی", "یکی بود یکی نبود", "بهار نارنج",
        ):
            with self.subTest(value=value):
                self.assertIsNone(ad_name_detector.reason(user(value)), value)


class AdvertisingNameEnforcementTests(unittest.TestCase):
    def setUp(self):
        message_tracker.reset_all()
        clear_user(CHAT, USER_ID)

    def tearDown(self):
        message_tracker.reset_all()
        clear_user(CHAT, USER_ID)

    def test_prior_and_triggering_messages_delete_before_punishment(self):
        async def scenario():
            bot = Bot()
            message_tracker.add_message(CHAT, USER_ID, 11, "پیام قبلی اول")
            message_tracker.add_message(CHAT, USER_ID, 12, "پیام قبلی دوم")
            save_history_message(CHAT, USER_ID, 10, "پیام قدیمی قابل حذف")

            with (
                patch.object(mh, "is_global_owner", return_value=False),
                patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
                patch.object(mh, "add_deleted_count", return_value=None),
                patch.object(mh.punishment_mode, "is_mute", return_value=False),
            ):
                handled = mh.enforce_advertising_name(
                    bot, CHAT, user("کانال"), current_message_id=13,
                    source="message",
                )
                self.assertTrue(handled)
                self.assertEqual(len(bot.moderation_queue.jobs), 1)
                job = bot.moderation_queue.jobs[0]
                result = await job["operation"]()
                self.assertTrue(result)

                # IDs from both existing histories plus the triggering message.
                requested = set().union(*(set(call[1]) for call in bot.message_delete_queue.calls))
                self.assertEqual(requested, {10, 11, 12, 13})
                self.assertEqual(bot.order[-1], ("punish", USER_ID))
                self.assertTrue(all(step[0] == "delete" for step in bot.order[:-1]))
                self.assertIn("نام تبلیغاتی", bot.admin_actions.calls[0][2]["reason"])

                await job["on_success"](result)
                self.assertTrue(any("AD NAME ACTION FINISHED" in x for x in bot.logger.infos))
                self.assertTrue(bot.client.sent)

        asyncio.run(scenario())

    def test_message_arriving_while_punishment_is_pending_is_also_deleted(self):
        async def scenario():
            bot = Bot()
            with (
                patch.object(mh, "is_global_owner", return_value=False),
                patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
                patch.object(mh.punishment_mode, "is_mute", return_value=False),
            ):
                self.assertTrue(mh.enforce_advertising_name(
                    bot, CHAT, user("کانال"), current_message_id=31,
                ))
                self.assertTrue(mh.enforce_advertising_name(
                    bot, CHAT, user("کانال"), current_message_id=32,
                ))
            self.assertEqual(len(bot.moderation_queue.jobs), 1)
            self.assertTrue(any(32 in call[1] for call in bot.message_delete_queue.calls))

        asyncio.run(scenario())

    def test_untracked_accessible_history_is_swept_before_punishment(self):
        async def scenario():
            history = [
                SimpleNamespace(id=21, sender_id=USER_ID),
                SimpleNamespace(id=22, sender_id=999999),
                SimpleNamespace(id=23, sender_id=USER_ID),
            ]
            bot = Bot(history)
            with (
                patch.object(mh, "is_global_owner", return_value=False),
                patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
                patch.object(mh, "add_deleted_count", return_value=None),
                patch.object(mh.punishment_mode, "is_mute", return_value=False),
            ):
                self.assertTrue(mh.enforce_advertising_name(
                    bot, CHAT, user("فیلم"), source="join",
                ))
                job = bot.moderation_queue.jobs[0]
                await job["operation"]()

            requested = set().union(*(set(call[1]) for call in bot.message_delete_queue.calls))
            self.assertEqual(requested, {21, 23})
            self.assertNotIn(22, requested)
            self.assertEqual(bot.order[-1], ("punish", USER_ID))
            self.assertTrue(all(step[0] == "delete" for step in bot.order[:-1]))

        asyncio.run(scenario())

    def test_name_change_after_join_is_checked_without_new_message(self):
        async def scenario():
            bot = Bot()
            # ChatAction join records this association while the name is clean.
            mh.remember_ad_name_group(bot, CHAT, USER_ID)
            update = SimpleNamespace(
                user_id=USER_ID,
                first_name="ب ی و گ ر ا ف ی",
                last_name=None,
                username=None,
            )
            with (
                patch.object(mh, "is_active", return_value=True),
                patch.object(mh, "is_global_owner", return_value=False),
                patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
                patch.object(mh.punishment_mode, "is_mute", return_value=False),
            ):
                queued = await mh.handle_ad_name_profile_update(bot, update)
            self.assertEqual(queued, 1)
            self.assertEqual(len(bot.moderation_queue.jobs), 1)
            self.assertEqual(bot.moderation_queue.jobs[0]["chat_id"], CHAT)

        asyncio.run(scenario())

    def test_username_list_change_after_join_is_checked(self):
        async def scenario():
            bot = Bot()
            mh.remember_ad_name_group(bot, CHAT, USER_ID)
            update = SimpleNamespace(
                user_id=USER_ID,
                first_name="علی",
                last_name="رضایی",
                usernames=[SimpleNamespace(username="ک_ا_ن_ا_ل", active=True)],
            )
            with (
                patch.object(mh, "is_active", return_value=True),
                patch.object(mh, "is_global_owner", return_value=False),
                patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
                patch.object(mh.punishment_mode, "is_mute", return_value=False),
            ):
                queued = await mh.handle_ad_name_profile_update(bot, update)
            self.assertEqual(queued, 1)
            self.assertEqual(len(bot.moderation_queue.jobs), 1)

        asyncio.run(scenario())

    def test_advertising_name_on_join_uses_same_existing_action(self):
        bot = Bot()
        with (
            patch.object(mh, "is_global_owner", return_value=False),
            patch.object(mh.admin_tools, "has_admin_permission", return_value=False),
            patch.object(mh.punishment_mode, "is_mute", return_value=True),
        ):
            handled = mh.enforce_advertising_name(
                bot, CHAT, user("د خ ت ر"), source="join",
            )
        self.assertTrue(handled)
        self.assertEqual(len(bot.moderation_queue.jobs), 1)
        self.assertEqual(bot.moderation_queue.jobs[0]["action"], "ban")
        # action remains "ban" because AdminActions.ban_user centrally selects
        # permanent mute via punishment_mode, preserving the existing policy.


if __name__ == "__main__":
    unittest.main(verbosity=2)
