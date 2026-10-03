"""Source outages preserve durable work and do not silence its queue."""
import contextlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pipeline_probe
import telegram_collector


class PipelineReliabilityTests(unittest.TestCase):
    def test_network_failure_preserves_collector_checkpoint_and_inbox(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            inbox = Path(directory) / "inbox.jsonl"
            state.write_text(json.dumps({"last_seen_id": 1748}))
            inbox.write_text('{"telegram_id": 1748, "status": "collected"}\n')
            before = (state.read_bytes(), inbox.read_bytes())
            with (
                patch.object(telegram_collector, "STATE_PATH", state),
                patch.object(telegram_collector, "INBOX_PATH", inbox),
                patch.object(telegram_collector.requests, "get", side_effect=
                    telegram_collector.requests.exceptions.ConnectionError("network unreachable")),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                result = telegram_collector.main()
            self.assertEqual(result, 1)
            self.assertEqual((state.read_bytes(), inbox.read_bytes()), before)

    def test_pending_facebook_queue_runs_without_requiring_telegram(self):
        def rows(path):
            return [{"telegram_id": 1748, "status": "ready"}] if path == pipeline_probe.READY else []
        with (
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}),
            patch.object(pipeline_probe, "read_json", return_value={"last_seen_id": 1748}),
            patch.object(pipeline_probe, "read_jsonl", side_effect=rows),
            patch.object(pipeline_probe, "latest_telegram_id", side_effect=OSError("offline")) as probe,
        ):
            needs_run, reason, details = pipeline_probe.decision()
        self.assertTrue(needs_run)
        self.assertEqual(reason, "pending_facebook_queue")
        self.assertEqual(details["pending_ids"], [1748])
        probe.assert_not_called()

    def test_failed_cheap_probe_runs_real_pipeline(self):
        with (
            patch.object(pipeline_probe, "decision", side_effect=OSError("offline")),
            patch.object(pipeline_probe, "write_output") as output,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(pipeline_probe.main(), 0)
        output.assert_any_call("needs_run", "true")
        output.assert_any_call("reason", "probe_failed_safe_full_run")

    def test_collector_failure_is_isolated_in_production_workflow(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/telegram-collector.yml").read_text()
        collector = workflow.split("- name: Collect every new Telegram post", 1)[1].split("- name:", 1)[0]
        self.assertIn("continue-on-error: true", collector)
        self.assertIn("id: collect", collector)
        self.assertIn("steps.collect.outcome", workflow)

    def test_service_failures_do_not_block_independent_preparation_or_queued_work(self):
        workflow = (Path(__file__).resolve().parents[1] /
                    ".github/workflows/telegram-collector.yml").read_text()
        for name, step_id in (
            ("Publish at most one eligible Facebook item before AI", "publish_before"),
            ("Prepare every unprocessed item in Arabic", "prepare"),
        ):
            step = workflow.split(f"- name: {name}", 1)[1].split("- name:", 1)[0]
            self.assertIn("continue-on-error: true", step)
            self.assertIn(f"id: {step_id}", step)
            self.assertIn(f"steps.{step_id}.outcome", workflow)
        after = workflow.split("- name: Publish at most one eligible Facebook item after AI", 1)[1].split("- name:", 1)[0]
        self.assertIn("steps.publish_before.outcome != 'failure'", after)


if __name__ == "__main__":
    unittest.main()
