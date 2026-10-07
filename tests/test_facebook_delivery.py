"""Delivery failures must stay retryable without creating a second post."""

import contextlib
import io
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import facebook_publisher as publisher


class FacebookDeliveryTests(unittest.TestCase):
    def item(self, **changes):
        row = {
            "telegram_id": 1800,
            "status": "ready",
            "language": "en",
            "source_published_at": publisher.now_iso(),
            "card_title": "",
            "facebook_post": "The company announced a new security update.\n\nIt fixes the disclosed vulnerability.",
            "first_comment": "",
            "source_url": "",
            "media": {},
        }
        row.update(changes)
        return row

    @contextlib.contextmanager
    def delivery(self, item):
        image = SimpleNamespace(
            origin="telegram", width=1600, height=900, url="test-image"
        )
        video = SimpleNamespace(
            width=1080, height=1920, duration=10, size=1024, branded=True
        )
        rights = SimpleNamespace(
            reupload_allowed=True, clip_start_seconds=0, max_clip_seconds=30,
            to_dict=lambda: {"reupload_allowed": True},
        )
        mocks = {}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "FACEBOOK_PAGE_ID": "test-page",
                "FACEBOOK_PAGE_ACCESS_TOKEN": "test-token",
                "FACEBOOK_TARGET_TELEGRAM_ID": "",
            }))
            for name, value in {
                "verify": {"id": "test-page", "name": "Test Page"},
                "state": ({}, None),
                "retry_comments": None,
                "read_jsonl": [item],
                "terminal_ids": set(),
                "find_duplicate_story": None,
                "recover_existing_remote_post": "",
                "telegram_image_candidates": [],
                "resolve_post_images": ([image], {}),
                "build_branded_fallback_asset": image,
                "upload_photo_bytes": "photo-1",
                "publish_images": "post-1",
                "publish_video_file": "video-1",
                "prepare_branded_video": video,
                "evaluate_video_rights": rights,
                "append_event": None,
            }.items():
                mocks[name] = stack.enter_context(
                    patch.object(publisher, name, return_value=value)
                )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            yield mocks

    def test_missing_card_title_uses_existing_ai_hook_for_the_fallback_image(self):
        for title in ("", "   "):
            with self.subTest(title=title):
                item = self.item(card_title=title)
                with self.delivery(item) as mocks:
                    mocks["resolve_post_images"].return_value = ([], {})
                    self.assertEqual(publisher.main(), 0)
                mocks["build_branded_fallback_asset"].assert_called_once_with(
                    "The company announced a new security update.", variant_key=1800
                )
                events = [call.args[0] for call in mocks["append_event"].call_args_list]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["facebook_post_id"], "post-1")

    def test_video_upload_timeout_does_not_publish_an_image_as_a_second_attempt(self):
        item = self.item(media={"has_video": True, "video_urls": ["test-video"]})
        with self.delivery(item) as mocks:
            mocks["publish_video_file"].side_effect = publisher.requests.exceptions.Timeout(
                "Meta may have accepted the video"
            )
            self.assertEqual(publisher.main(), 1)
        mocks["resolve_post_images"].assert_not_called()
        mocks["publish_images"].assert_not_called()
        mocks["append_event"].assert_not_called()

    def test_video_preparation_failure_can_still_use_the_image(self):
        item = self.item(media={"has_video": True, "video_urls": ["test-video"]})
        with self.delivery(item) as mocks:
            mocks["prepare_branded_video"].side_effect = OSError("download failed")
            self.assertEqual(publisher.main(), 0)
        mocks["publish_video_file"].assert_not_called()
        mocks["publish_images"].assert_called_once()

    def test_successful_video_has_one_publication_event_and_no_image_attempt(self):
        item = self.item(media={"has_video": True, "video_urls": ["test-video"]})
        with self.delivery(item) as mocks:
            self.assertEqual(publisher.main(), 0)
        mocks["publish_video_file"].assert_called_once()
        mocks["resolve_post_images"].assert_not_called()
        mocks["append_event"].assert_called_once()
        self.assertEqual(mocks["append_event"].call_args.args[0]["media_mode"], "video")

    def test_failed_recovery_lookup_is_not_treated_as_proof_of_no_existing_post(self):
        with patch.object(publisher, "api", side_effect=OSError("lookup offline")):
            with self.assertRaisesRegex(RuntimeError, "recovery lookup"):
                publisher.recover_existing_remote_post("page", "token", "نص المنشور")

    def test_incomplete_recovery_response_is_not_proof_of_no_existing_post(self):
        with patch.object(publisher, "api", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "recovery lookup"):
                publisher.recover_existing_remote_post("page", "token", "نص المنشور")

    def test_empty_facebook_feed_acknowledgment_is_a_retryable_failure(self):
        with patch.object(publisher, "api", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "post ID"):
                publisher.publish_images("page", "token", "نص المنشور", ["photo-1"])

    def test_comment_without_an_acknowledged_id_remains_pending(self):
        with (
            patch.object(publisher, "api", return_value={}),
            patch.object(publisher, "append_event") as events,
            contextlib.redirect_stderr(io.StringIO()),
        ):
            status = publisher.publish_first_comment(1800, "post-1", "token", "المصدر")
        self.assertEqual(status, "pending_retry")
        events.assert_not_called()


if __name__ == "__main__":
    unittest.main()

class QueueEligibilityTests(FacebookDeliveryTests):
    def test_empty_legacy_row_does_not_block_valid_english_item(self):
        valid=self.item()
        empty=self.item(telegram_id=1750,facebook_post='',language='ar')
        with self.delivery(valid) as mocks:
            mocks['read_jsonl'].side_effect=lambda path: [empty,valid] if path==publisher.READY else []
            self.assertEqual(publisher.main(),0)
        mocks['publish_images'].assert_called_once()
        self.assertTrue(any(c.args[0].get('event')=='editorial_hold' for c in mocks['append_event'].call_args_list))

    def test_persistent_cooldown_prevents_even_verification_request(self):
        from datetime import timedelta
        with self.delivery(self.item()) as mocks:
            mocks['read_jsonl'].return_value=[{'event':'meta_cooldown','retry_at':(publisher.now()+timedelta(minutes=10)).isoformat()}]
            self.assertEqual(publisher.main(),0)
        mocks['verify'].assert_not_called()
        mocks['publish_images'].assert_not_called()
