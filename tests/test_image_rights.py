import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import image_resolver
import image_rights

class ImageRightsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.policy_path = Path(self.temp.name) / "image_rights.json"
        self.write_policy({"version": 1, "default_status": "unknown", "channel_policies": {}, "post_overrides": {}})

    def write_policy(self, value):
        self.policy_path.write_text(json.dumps(value), encoding="utf-8")

    def evaluate(self, item, origin):
        with patch.object(image_rights, "POLICY_PATH", self.policy_path):
            return image_rights.evaluate_image_rights(item, origin)

    def test_unknown_rights_deny_telegram_and_source_images(self):
        item = {"telegram_id": 12, "media": {"telegram_post_url": "https://t.me/IntCyberDigest/12"}, "source_url": "https://vendor.example/security-update"}
        self.assertFalse(self.evaluate(item, "telegram").reuse_allowed)
        self.assertFalse(self.evaluate(item, "source").reuse_allowed)

    def test_licensed_image_requires_https_license_url_and_credit(self):
        item = {"telegram_id": 12, "media": {"image_rights": {"telegram": {
            "status": "licensed", "reuse_allowed": True, "basis": "reviewed license",
            "license_url": "http://license.example"}}}}
        decision = self.evaluate(item, "telegram")
        self.assertFalse(decision.reuse_allowed)
        self.assertEqual(decision.basis, "reuse_rule_missing_required_provenance_metadata")

    def test_valid_licensed_image_is_allowed_with_credit_metadata(self):
        item = {"telegram_id": 12, "media": {"image_rights": {"telegram": {
            "status": "licensed", "reuse_allowed": True, "basis": "reviewed file license",
            "credit": "Jane Example", "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "license_note": "CC BY 4.0"}}}}
        decision = self.evaluate(item, "telegram")
        self.assertTrue(decision.reuse_allowed)
        self.assertEqual(decision.credit, "Jane Example")
        self.assertTrue(decision.license_url.startswith("https://"))

    def test_channel_permission_never_authorizes_linked_source_image(self):
        self.write_policy({"version": 1, "default_status": "unknown",
            "channel_policies": {"IntCyberDigest": {
                "status": "owned", "reuse_allowed": True, "basis": "page-owned photos reviewed by editor"}},
            "post_overrides": {}})
        item = {"telegram_id": 12, "media": {"telegram_post_url": "https://t.me/IntCyberDigest/12"}}
        self.assertTrue(self.evaluate(item, "telegram").reuse_allowed)
        self.assertFalse(self.evaluate(item, "source").reuse_allowed)

    def test_unknown_rights_skip_download_and_source_scraping(self):
        with (patch.object(image_resolver, "fetch_image") as fetch,
              patch.object(image_resolver, "source_image_candidates") as source_candidates):
            assets, diagnostics = image_resolver.resolve_post_images(
                ["https://cdn.example/photo.jpg"], "https://vendor.example/article",
                allow_telegram_images=False, allow_source_images=False)
        self.assertEqual(assets, [])
        fetch.assert_not_called()
        source_candidates.assert_not_called()
        self.assertTrue(diagnostics["telegram_skipped_by_rights"])
        self.assertTrue(diagnostics["source_skipped_by_rights"])

    def test_explicitly_allowed_source_uses_one_best_hero_image(self):
        candidates = [
            SimpleNamespace(url="https://vendor.example/one.jpg", origin="source", width=1200, height=630, score=10),
            SimpleNamespace(url="https://vendor.example/two.jpg", origin="source", width=1600, height=900, score=20)]
        with (patch.object(image_resolver, "source_image_candidates",
                           return_value=["https://vendor.example/one.jpg", "https://vendor.example/two.jpg"]),
              patch.object(image_resolver, "fetch_image", side_effect=candidates)):
            assets, diagnostics = image_resolver.resolve_post_images([], "https://vendor.example/article", allow_source_images=True)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0].url, "https://vendor.example/two.jpg")
        self.assertEqual(diagnostics["selected"][0]["origin"], "source")

if __name__ == "__main__":
    unittest.main()
