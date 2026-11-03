"""Unit tests: config parsing, verdict, classification, and route A end to end on the mock API."""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
ACTION = HERE.parent / ".github" / "actions" / "mozark-run"
sys.path.insert(0, str(ACTION))

import mock_api  # noqa: E402
import mozark_run as mr  # noqa: E402

CONFIG = {
    "project-id": "proj-1",
    "suites": {"tag": ["smoke"], "release": ["smoke", "regression"], "default": ["smoke"]},
    "platforms": {"android": {"devices": ["A1"], "timeout-minutes": 5}, "webos": {"device-group": "tray-tv"}},
}


class ConfigTests(unittest.TestCase):
    def test_trigger(self):
        self.assertEqual(mr.detect_trigger("push", "refs/tags/v0.1.0"), "tag")
        self.assertEqual(mr.detect_trigger("release", "refs/tags/v1"), "release")
        self.assertEqual(mr.detect_trigger("workflow_dispatch", "refs/heads/main"), "dispatch")
        self.assertEqual(mr.detect_trigger("push", "refs/heads/main"), "push")

    def test_suite_per_trigger(self):
        self.assertEqual(mr.resolve_spec(CONFIG, "android", "tag", {}).tags, ["smoke"])
        self.assertEqual(mr.resolve_spec(CONFIG, "android", "release", {}).tags, ["smoke", "regression"])
        self.assertEqual(mr.resolve_spec(CONFIG, "android", "push", {}).tags, ["smoke"])

    def test_inputs_override_config(self):
        spec = mr.resolve_spec(CONFIG, "android", "tag", {"suite": "regression", "devices": "X,Y", "timeout_minutes": "9"})
        self.assertEqual((spec.tags, spec.devices, spec.timeout_minutes), (["regression"], ["X", "Y"], 9))

    def test_device_group(self):
        spec = mr.resolve_spec(CONFIG, "webos", "tag", {})
        self.assertEqual((spec.devices, spec.device_group), ([], "tray-tv"))

    def test_missing_devices_is_ci_error(self):
        with self.assertRaises(mr.MozarkError) as e:
            mr.resolve_spec({"project-id": "p"}, "android", "tag", {})
        self.assertEqual(e.exception.failure_class, mr.CI)

    def test_unknown_platform(self):
        with self.assertRaises(mr.MozarkError):
            mr.resolve_spec(CONFIG, "roku", "tag", {})

    def test_host_guard(self):
        mr.check_host("https://app-studio-be.preprod.mozark.ai", False)
        mr.check_host("https://x.dev.mozark.ai/api", False)
        for bad in ("https://tenant-api.mozark.ai", "https://app.mozark.ai", "http://a.preprod.mozark.ai",
                    "https://preprod.mozark.ai.evil.com", "http://127.0.0.1:8765"):
            with self.assertRaises(mr.MozarkError, msg=bad):
                mr.check_host(bad, False)
        mr.check_host("http://127.0.0.1:8765", True)


def report(status, *rows, **data):
    return {"data": {"jobStatus": status, "testCases": list(rows), **data}}


def row(name, status, reason=""):
    return {"type": "test case", "testCaseName": name, "deviceSerial": "A1", "testCaseStatus": status, "reason": reason}


class VerdictTests(unittest.TestCase):
    def test_pass(self):
        o = mr.decide(report("Completed", row("a", "PASS"), row("b", "passed")))
        self.assertEqual(o.exit_code, mr.PASS)

    def test_completed_is_not_pass(self):
        o = mr.decide(report("Completed", row("a", "PASS"), row("b", "FAIL", "Element not found: id=play")))
        self.assertEqual((o.exit_code, o.failure_class), (mr.FAIL, mr.FRAMEWORK))

    def test_classes(self):
        cases = {
            "Element not found: xpath=//x": mr.FRAMEWORK,
            "Unresolved variable ${term} in dataset": mr.DATA,
            "Application not responding": mr.APP,
            "something odd": mr.UNCLASSIFIED,
        }
        for msg, cls in cases.items():
            o = mr.decide(report("Completed", row("a", "FAIL", msg)))
            self.assertEqual((o.exit_code, o.failure_class), (mr.FAIL, cls), msg)

    def test_infra_only_failure_is_exit_2(self):
        o = mr.decide(report("Completed", row("a", "FAIL", "Force Aborted: controller lost")))
        self.assertEqual((o.exit_code, o.failure_class), (mr.INFRA, mr.LAB))

    def test_nothing_ran(self):
        o = mr.decide(report("Failed", row("a", "NOT YET STARTED"), testReason="No free devices after 10 min"))
        self.assertEqual((o.exit_code, o.failure_class), (mr.INFRA, mr.PLATFORM))

    def test_aborted(self):
        o = mr.decide(report("Force Aborted"))
        self.assertEqual((o.exit_code, o.failure_class), (mr.INFRA, mr.LAB))

    def test_deterministic(self):
        r = report("Completed", row("a", "PASS"), row("b", "FAIL", "locator"))
        self.assertEqual(mr.decide(r), mr.decide(r))


class EndToEndTests(unittest.TestCase):
    """Route A against the mock API: upload, tags, lint, run, poll, JUnit."""

    def run_scenario(self, scenario, port):
        httpd = mock_api.serve(port, scenario)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        tmp = Path(tempfile.mkdtemp())
        build = tmp / "app.apk"
        build.write_bytes(b"PK\x03\x04fake")
        cfg = tmp / "mozark.yml"
        cfg.write_text("project-id: proj-1\nsuites: {default: [smoke]}\nplatforms: {android: {devices: [A1], timeout-minutes: 1}}\n")
        env = {"MOZARK_TOKEN": "t", "MOZARK_BASE_URL": f"http://127.0.0.1:{port}", "MOZARK_ORG_ID": "o",
               "MOZARK_APP_ID": "a", "MOZARK_MOCK": "1", "MOZARK_RETRY_DELAY": "0"}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        mr.time.sleep = lambda s: None  # fast polling
        args = mr.parse_args(["--build-path", str(build), "--platform", "android", "--config", str(cfg),
                              "--results-dir", str(tmp / "results")])
        return mr.run(args), tmp / "results"

    def test_pass(self):
        code, out = self.run_scenario("pass", 18761)
        self.assertEqual(code, mr.PASS)
        suite = ET.parse(out / "junit.xml").getroot().find("testsuite")
        self.assertEqual((suite.get("tests"), suite.get("failures")), ("2", "0"))  # smoke = 2 test cases

    def test_fail(self):
        code, out = self.run_scenario("fail", 18762)
        self.assertEqual(code, mr.FAIL)
        self.assertIn("automation-framework", (out / "summary.md").read_text())

    def test_flaky_infra_retries_then_passes(self):
        code, out = self.run_scenario("flaky-infra", 18763)
        self.assertEqual(code, mr.PASS)
        self.assertIn("attempts", (out / "summary.md").read_text())

    def test_bad_token_is_ci_integration(self):
        code, out = self.run_scenario("auth", 18764)
        self.assertEqual(code, mr.INFRA)
        self.assertIn("ci-integration", (out / "summary.md").read_text())


if __name__ == "__main__":
    unittest.main()
