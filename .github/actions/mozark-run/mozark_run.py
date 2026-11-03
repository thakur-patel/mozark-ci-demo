#!/usr/bin/env python3
"""Run a Mozark Test Studio project against a freshly built app, from CI.

Flow, every run:
  1. build file ID   upload the new build (never reuse an old one)
  2. script file ID  the project's latest saved scripts, packaged fresh (never cached)
  3. submit          one job with both file IDs, devices and job parameters
  4. poll            backoff, hard timeout
  5. report          results/junit.xml, results/summary.md, raw JSON, exit code

Exit codes: 0 pass, 1 test failure, 2 infra error, 3 timeout/unknown.

Routes (see contract.md):
  run-api  App Studio POST /jobs/:projectId/run packages and submits server-side.
  package  Export + codegen give the script file ID; the Action submits to the
           job runner itself (testFramework "optics").
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
import yaml

PASS, FAIL, INFRA, TIMEOUT = 0, 1, 2, 3

PLATFORMS = ("android", "android-tv", "ios", "webos", "tizen")
# Platforms Studio packages for. android-tv, webos and tizen are assumed to be
# added to App Studio (they aren't on develop as of 2026-11-03).
SUPPORTED_PLATFORMS = set(PLATFORMS)

WAITING = {"scheduled", "queued", "pending", "not yet started"}
IN_FLIGHT = {"started", "running", "in progress"}
TERMINAL = {"completed", "passed", "failed", "error", "aborted", "force aborted"}

VERDICTS = {
    "pass": "PASS", "passed": "PASS",
    "fail": "FAIL", "failed": "FAIL", "error": "FAIL", "broken": "FAIL",
    "skip": "SKIP", "skipped": "SKIP",
    "not run": "DID NOT RUN", "did_not_run": "DID NOT RUN", "did not run": "DID NOT RUN",
    "did not execute": "DID NOT RUN", "not yet started": "DID NOT RUN",
}

# Failure classes. Each rule needs a signal; nothing is forced into a class.
CI = "ci-integration"
PLATFORM = "platform-device"
LAB = "lab-connectivity"
FRAMEWORK = "automation-framework"
DATA = "data"
APP = "app"
UNCLASSIFIED = "unclassified"
INFRA_CLASSES = {CI, PLATFORM, LAB}
RETRYABLE = {PLATFORM, LAB}
MAX_INFRA_RETRIES = 2

# Text signals, checked in order. Lower-cased substring or regex match.
CLASS_RULES: list[tuple[str, re.Pattern[str]]] = [
    (PLATFORM, re.compile(r"max queue size|no free device|device(s)? (is |are )?busy|slot clash|device not found|offline|unsupported platform")),
    (LAB, re.compile(r"force aborted|connection (reset|refused)|timed? ?out|network|controller (lost|disconnected)|suite bundle failed to download")),
    (APP, re.compile(r"\bcrash|anr\b|application not responding|has stopped|install(ation)? failed|install_failed")),
    (DATA, re.compile(r"dataset|data ?table|test data|unresolved variable|\$\{[^}]+\}|keyerror|no such row")),
    (FRAMEWORK, re.compile(r"element not found|nosuchelement|locator|xpath|unable to locate|e0101|optics")),
]


class MozarkError(Exception):
    """A failure before or around the run, already classified."""

    def __init__(self, message: str, failure_class: str):
        super().__init__(message)
        self.failure_class = failure_class


# ---------------------------------------------------------------- config

@dataclasses.dataclass
class RunSpec:
    project_id: str
    platform: str
    trigger: str
    tags: list[str]
    test_case_ids: list[str]
    devices: list[str]
    device_group: str
    timeout_minutes: int
    max_test_minutes: int


def detect_trigger(event_name: str, ref: str) -> str:
    if event_name == "release":
        return "release"
    if event_name == "workflow_dispatch":
        return "dispatch"
    if event_name == "push" and ref.startswith("refs/tags/"):
        return "tag"
    return event_name or "local"


def split_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise MozarkError(f"{path} must be a mapping", CI)
    return data


def resolve_spec(config: dict, platform: str, trigger: str, inputs: dict) -> RunSpec:
    """Merge mozark.yml with Action inputs. Inputs win."""
    if platform not in PLATFORMS:
        raise MozarkError(f"platform '{platform}' is not one of {', '.join(PLATFORMS)}", CI)
    pconf = (config.get("platforms") or {}).get(platform) or {}
    suites = pconf.get("suites") or config.get("suites") or {}

    project_id = inputs.get("project_id") or str(config.get("project-id") or "")
    if not project_id:
        raise MozarkError("project-id is missing (Action input or mozark.yml)", CI)

    tags = split_list(inputs.get("suite")) or split_list(suites.get(trigger)) or split_list(suites.get("default"))
    devices = split_list(inputs.get("devices")) or split_list(pconf.get("devices"))
    device_group = inputs.get("device_group") or str(pconf.get("device-group") or "")
    if not devices and not device_group:
        raise MozarkError(f"no devices or device-group for platform '{platform}'", CI)

    timeout = int(inputs.get("timeout_minutes") or pconf.get("timeout-minutes") or 45)
    max_test = int(pconf.get("max-test-minutes") or 10)
    return RunSpec(project_id, platform, trigger, tags, split_list(inputs.get("test_case_ids")),
                   devices, device_group, timeout, max_test)


def check_host(url: str, allow_local: bool) -> None:
    """Hard rule: preprod or dev only. Localhost only for the built-in mock."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if allow_local and host in ("127.0.0.1", "localhost"):
        return
    if parsed.scheme != "https" or not re.search(r"\.(preprod|dev)\.mozark\.ai$", host):
        raise MozarkError(f"refusing host '{host}': only *.preprod.mozark.ai or *.dev.mozark.ai", CI)


# ---------------------------------------------------------------- HTTP

class Api:
    def __init__(self, base: str, headers: dict[str, str], where: str, session: requests.Session | None = None):
        self.base = base.rstrip("/")
        self.headers = headers
        self.where = where
        self.s = session or requests.Session()

    def call(self, method: str, path: str, *, timeout: float = 60, raw: bool = False, **kw) -> Any:
        url = f"{self.base}{path}"
        try:
            res = self.s.request(method, url, headers={**self.headers, **kw.pop("headers", {})}, timeout=timeout, **kw)
        except (requests.ConnectionError, requests.Timeout) as e:
            raise MozarkError(f"{self.where} {method} {path}: network error {type(e).__name__}", LAB) from e
        if res.status_code in (401, 403):
            raise MozarkError(f"{self.where} {method} {path}: HTTP {res.status_code} (token or project access)", CI)
        if 400 <= res.status_code < 500:
            raise MozarkError(f"{self.where} {method} {path}: HTTP {res.status_code} {res.text[:300]}", classify_text(res.text, CI))
        if res.status_code >= 500:
            raise MozarkError(f"{self.where} {method} {path}: HTTP {res.status_code}", LAB)
        if raw:
            return res.content
        return res.json() if res.content else {}


def classify_text(text: str, default: str = UNCLASSIFIED) -> str:
    low = (text or "").lower()
    for cls, rule in CLASS_RULES:
        if rule.search(low):
            return cls
    return default


# ---------------------------------------------------------------- Mozark calls

class Mozark:
    def __init__(self, args: argparse.Namespace, spec: RunSpec):
        auth = {"Authorization": f"Bearer {args.token}", "Accept": "application/json"}
        self.studio = Api(args.base_url, {**auth, "x-org-id": args.org_id, "x-app-id": args.app_id,
                                          "x-project-id": spec.project_id}, "app-studio")
        self.runner = Api(args.runner_url, {**auth, "tenant": args.tenant}, "job-runner") if args.runner_url else None
        self.files_url = args.files_url
        self.acp_url = args.acp_url
        self.token = args.token
        self.tenant = args.tenant
        self.spec = spec
        self.route_used = ""

    # 1. build file ID
    def get_build_file_id(self, build_path: Path) -> dict:
        if not build_path.exists():
            raise MozarkError(f"build not found: {build_path}", CI)
        with build_path.open("rb") as f:
            up = self.studio.call("POST", "/app-uploads", files={"file": (build_path.name, f)}, timeout=900)
        deadline = time.time() + 900
        while up.get("status") == "processing":  # iOS resign
            if time.time() > deadline:
                raise MozarkError("build still processing after 15 min", PLATFORM)
            time.sleep(15)
            up = self.studio.call("GET", f"/app-uploads/{up['id']}/status")
        if up.get("status") != "ready":
            raise MozarkError(f"build upload status {up.get('status')}", PLATFORM)
        return up

    def resolve_test_case_ids(self) -> list[str]:
        if self.spec.test_case_ids:
            return self.spec.test_case_ids
        if not self.spec.tags:
            return []  # whole project
        rows = self.studio.call("GET", "/test-cases", params={"tags": ",".join(self.spec.tags)})
        ids = [r["id"] for r in rows if isinstance(r, dict) and r.get("id")]
        if not ids:
            raise MozarkError(f"no test cases tagged {', '.join(self.spec.tags)}", CI)
        return ids

    def lint(self) -> None:
        errors = self.studio.call("GET", f"/optics-export/{self.spec.project_id}/lint").get("errors") or []
        if errors:
            raise MozarkError(f"project lint failed: {len(errors)} error(s): {json.dumps(errors)[:500]}", FRAMEWORK)

    # 2. script file ID (package route). Each route is its own function so the
    # fallbacks can go once a single Package API exists.
    def get_script_file_id(self, ids: list[str], suite_name: str, scripts_zip: str) -> str:
        routes: list[tuple[str, Callable[[], str]]] = [
            ("package-api", lambda: self._script_via_package(ids, suite_name)),
            ("export-zip+upload", lambda: self._script_via_export_zip(ids, suite_name)),
        ]
        if scripts_zip:
            routes.append(("manual-zip+upload", lambda: self._upload_to_files_api(Path(scripts_zip), "optics-test-application")))
        last: MozarkError | None = None
        for name, fn in routes:
            try:
                file_id = fn()
                self.route_used = name
                log(f"script route: {name} -> fileId {file_id}")
                return file_id
            except MozarkError as e:
                log(f"script route {name} failed: {e}")
                last = e
        raise last or MozarkError("no script route available", CI)

    def _export(self, ids: list[str]) -> list[dict]:
        body = {"testCaseIds": ids} if ids else {}
        pkg = self.studio.call("POST", f"/optics-export/{self.spec.project_id}/export",
                               params={"platform": self.spec.platform}, json=body)
        return [{"path": p, "content": c} for p, c in (pkg.get("files") or {}).items()]

    def _script_via_package(self, ids: list[str], suite_name: str) -> str:
        files = self._export(ids)
        out = self.studio.call("POST", "/codegen/pytest", timeout=600, json={
            "testSuiteName": suite_name, "parentProjectId": self.spec.project_id, "dataFiles": files})
        return out["fileId"]

    def _script_via_export_zip(self, ids: list[str], suite_name: str) -> str:
        files = self._export(ids)
        zip_bytes = self.studio.call("POST", "/codegen/pytest/download", raw=True, timeout=600, json={
            "testSuiteName": suite_name, "parentProjectId": self.spec.project_id, "dataFiles": files})
        path = Path("results") / f"{suite_name}.zip"
        path.write_bytes(zip_bytes)
        return self._upload_to_files_api(path, "appium-python-test-application")

    def _upload_to_files_api(self, path: Path, category: str) -> str:
        if not (self.files_url and self.acp_url):
            raise MozarkError("files-api-url and acp-url are needed to upload to files-api", CI)
        acp = Api(self.acp_url, {"authorization": f"Bearer {self.token}"}, "acp")
        tok = acp.call("POST", "/issue_temperory_token")  # spelt this way server-side
        files_api = Api(self.files_url, {"authorization": f"Bearer {tok.get('idToken') or tok.get('token')}"}, "files-api")
        meta = {"source": "mozark-ci", "fileCategory": category, "testType": "app-automation",
                "projectId": self.spec.project_id}
        with path.open("rb") as f:
            body = files_api.call("POST", "/", timeout=600, data={"organizationId": self.tenant, "meta": json.dumps(meta)},
                                  files={"upload_file": (path.name, f)})
        file_id = body.get("fileId") or body.get("id")
        if not file_id:
            raise MozarkError("files-api returned no fileId", LAB)
        return file_id

    # 3. submit
    def submit(self, route: str, upload: dict, ids: list[str], suite_name: str, scripts_zip: str, ci_meta: dict) -> dict:
        if route == "run-api":
            body = {"platform": self.spec.platform, "devices": self.spec.devices, "appUploadId": upload["id"],
                    "installApp": True, "mode": "full", "maxTestDuration": self.spec.max_test_minutes,
                    "suiteName": suite_name}
            if ids:
                body["testCaseIds"] = ids
            # RunJobDto refuses unknown fields, so ci_meta stays in the summary.
            out = self.studio.call("POST", f"/jobs/{self.spec.project_id}/run", json=body)
            self.route_used = "run-api"
            return {"job_id": out.get("jobId"), "script_file_id": (out.get("artifact") or {}).get("fileId")}

        if not self.runner:
            raise MozarkError("route 'package' needs runner-url", CI)
        script_id = self.get_script_file_id(ids, suite_name, scripts_zip)
        payload = runner_payload(self.spec, upload, script_id, suite_name, ci_meta)
        out = self.runner.call("POST", "/job", json=payload)
        job_id = out.get("jobId") or (out.get("data") or {}).get("jobId") if isinstance(out, dict) else None
        return {"job_id": job_id, "script_file_id": script_id}

    def report(self, job_id: str) -> dict:
        if self.route_used == "run-api" or not self.runner:
            return self.studio.call("GET", f"/jobs/{job_id}/report")
        return self.runner.call("GET", f"/job/{job_id}", params={"withTestCases": "true"})

    def tests(self, job_id: str) -> Any:
        if self.route_used == "run-api" or not self.runner:
            return self.studio.call("GET", f"/jobs/{job_id}/tests")
        return self.runner.call("GET", f"/job/{job_id}", params={"showTests": "true"})

    def session_junit(self, test_id: str) -> bytes | None:
        """Optics junit_output.xml from a session's output files.

        [ASSUMPTION] Optics is a job runner + controller framework and its JUnit
        lands in the session's `output` files under this name.
        """
        listing = self.studio.call("GET", f"/sessions/{test_id}/files", params={"type": "output"})
        names = [n for n in json.dumps(listing).split('"') if n.endswith("junit_output.xml")]
        if not names:
            return None
        return self.studio.call("GET", f"/sessions/{test_id}/files/content", raw=True,
                                params={"type": "output", "fileName": names[0]})


def runner_payload(spec: RunSpec, upload: dict, script_id: str, suite_name: str, ci_meta: dict) -> dict:
    """POST /job body, same shape as App Studio's buildJobRunnerPayload."""
    device_config: dict[str, Any] = {"devices": spec.devices}
    if spec.device_group:
        device_config["trayId"] = spec.device_group
    return {
        "testDataParameters": {},
        "deviceConfig": device_config,
        "runnerConfig": {
            "testConfiguration": {
                "recordDeviceScreen": True, "captureDeviceScreenShots": True, "captureDeviceLogs": True,
                "captureNetworkLogs": True, "streamDeviceVideo": True, "streamDeviceLogs": True,
            },
            "testParameters": {
                "testApplicationUrl": f"mzfile://{script_id}",
                "executionName": suite_name, "jobIdentifier": suite_name,
                "testFramework": "optics",  # [ASSUMPTION] Optics as a runner framework
                "testType": "app-automation", "packageName": upload.get("packageName") or "default",
                "executeAllTests": True, "testCases": "", "maxJobDuration": spec.timeout_minutes,
                "maxTestDuration": spec.max_test_minutes, "mode": "full",
                # [ASSUMPTION] free-form keys round-trip on GET /job/:id
                "ci": ci_meta,
            },
            "applicationUrl": f"mzfile://{upload['fileId']}",
            "testApplicationUrl": f"mzfile://{script_id}",
            "type": "symmetrical", "retry": False, "maxRetry": 0,
        },
    }


# ---------------------------------------------------------------- verdict

@dataclasses.dataclass
class TestResult:
    name: str
    device: str
    verdict: str
    message: str = ""
    failure_class: str = ""


@dataclasses.dataclass
class Outcome:
    exit_code: int
    status: str
    failure_class: str
    reason: str
    tests: list[TestResult]


def job_status(report: dict) -> str:
    data = report.get("data") if isinstance(report, dict) else None
    if isinstance(data, list):
        data = data[0] if data else {}
    return str((data or {}).get("jobStatus") or "").strip().lower()


def test_rows(report: dict) -> list[dict]:
    data = report.get("data") if isinstance(report, dict) else None
    rows = (data or {}).get("testCases") if isinstance(data, dict) else None
    rows = [r for r in rows or [] if isinstance(r, dict)]
    cases = [r for r in rows if r.get("type") in (None, "test case")]
    return cases or rows


def row_message(row: dict) -> str:
    parts = [str(row.get(k)) for k in ("reason", "failureReason", "error", "message", "testReason") if row.get(k)]
    for d in row.get("details") or []:
        if isinstance(d, dict):
            parts += [str(d.get(k)) for k in ("reason", "error", "message") if d.get(k)]
    return " | ".join(parts)


def decide(report: dict) -> Outcome:
    """Turn a terminal runner report into a verdict. Deterministic: same report, same outcome."""
    status = job_status(report)
    data = report.get("data") if isinstance(report.get("data"), dict) else {}
    job_reason = " ".join(str(data.get(k)) for k in ("testReason", "reason", "failureReason") if data.get(k))
    tests = []
    for r in test_rows(report):
        verdict = VERDICTS.get(str(r.get("testCaseStatus") or r.get("testStatus") or "").strip().lower(), "DID NOT RUN")
        msg = row_message(r)
        cls = ""
        if verdict == "FAIL":
            cls = classify_text(msg, UNCLASSIFIED)
        elif verdict == "DID NOT RUN":
            cls = classify_text(msg + " " + job_reason, UNCLASSIFIED)
        tests.append(TestResult(str(r.get("testCaseName") or r.get("path") or "?"), str(r.get("deviceSerial") or ""),
                                verdict, msg, cls))

    failed = [t for t in tests if t.verdict == "FAIL"]
    ran = [t for t in tests if t.verdict in ("PASS", "FAIL")]

    if failed:
        # A failure whose only signal is infra still counts as infra, so it can be retried.
        if all(t.failure_class in INFRA_CLASSES for t in failed):
            return Outcome(INFRA, status, failed[0].failure_class, failed[0].message, tests)
        cls = next((t.failure_class for t in failed if t.failure_class not in INFRA_CLASSES), UNCLASSIFIED)
        return Outcome(FAIL, status, cls, f"{len(failed)} of {len(tests)} test(s) failed", tests)
    if status in ("aborted", "force aborted", "error", "failed") and not ran:
        cls = classify_text(job_reason, LAB if status == "force aborted" else PLATFORM)
        return Outcome(INFRA, status, cls, job_reason or f"job {status} before any test ran", tests)
    if not ran:
        cls = classify_text(job_reason, PLATFORM)
        return Outcome(INFRA, status, cls, job_reason or "no test ran", tests)
    if len(ran) < len(tests):
        dnr = [t for t in tests if t.verdict == "DID NOT RUN"]
        return Outcome(INFRA, status, dnr[0].failure_class or PLATFORM, f"{len(dnr)} test(s) did not run", tests)
    return Outcome(PASS, status, "", f"{len(ran)} test(s) passed", tests)


# ---------------------------------------------------------------- outputs

def write_junit(path: Path, suite: str, outcome: Outcome, meta: dict) -> None:
    root = ET.Element("testsuites", name=suite)
    ts = ET.SubElement(root, "testsuite", name=suite, tests=str(len(outcome.tests)),
                       failures=str(sum(t.verdict == "FAIL" for t in outcome.tests)),
                       skipped=str(sum(t.verdict in ("SKIP", "DID NOT RUN") for t in outcome.tests)))
    props = ET.SubElement(ts, "properties")
    for k, v in meta.items():
        ET.SubElement(props, "property", name=k, value=str(v))
    for t in outcome.tests:
        tc = ET.SubElement(ts, "testcase", name=t.name, classname=f"{meta.get('platform')}.{t.device or 'device'}")
        if t.verdict == "FAIL":
            ET.SubElement(tc, "failure", message=t.message[:500] or "failed", type=t.failure_class or UNCLASSIFIED)
        elif t.verdict in ("SKIP", "DID NOT RUN"):
            ET.SubElement(tc, "skipped", message=t.verdict)
    if not outcome.tests and outcome.exit_code != PASS:
        tc = ET.SubElement(ts, "testcase", name="mozark-run", classname=str(meta.get("platform")))
        ET.SubElement(tc, "error", message=outcome.reason[:500], type=outcome.failure_class or UNCLASSIFIED)
    ET.indent(root)
    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


EXIT_LABEL = {PASS: "✅ pass", FAIL: "❌ test failure", INFRA: "⚠️ infra error", TIMEOUT: "⏱️ timeout / unknown"}


def write_summary(path: Path, outcome: Outcome, meta: dict, attempts: list[str]) -> str:
    lines = [f"## Mozark run · {meta.get('platform')} · {EXIT_LABEL[outcome.exit_code]}", "",
             "| | |", "|---|---|"]
    for k in ("app_version", "commit", "trigger", "suite_tags", "devices", "job_id", "build_file_id",
              "script_file_id", "script_route", "idempotency_key", "report_url", "ci_run_url", "duration_s"):
        if meta.get(k):
            lines.append(f"| {k.replace('_', ' ')} | {meta[k]} |")
    lines.append(f"| verdict | exit {outcome.exit_code}: {outcome.reason} |")
    if outcome.failure_class:
        lines.append(f"| failure class | `{outcome.failure_class}` |")
    if len(attempts) > 1:
        lines.append(f"| attempts | {' → '.join(attempts)} |")
    if outcome.tests:
        lines += ["", "| Test | Device | Verdict | Class | Message |", "|---|---|---|---|---|"]
        for t in outcome.tests:
            lines.append(f"| {t.name} | {t.device} | {t.verdict} | {t.failure_class} | {t.message[:120].replace('|', '/')} |")
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    return text


def log(msg: str) -> None:
    print(f"[mozark-run] {msg}", flush=True)


def set_output(name: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"{name}={value}\n")


# ---------------------------------------------------------------- main

def poll(mz: Mozark, job_id: str, timeout_minutes: int, sleep: Callable[[float], None] | None = None) -> dict | None:
    sleep = sleep or time.sleep
    deadline = time.time() + timeout_minutes * 60
    delay, last = 5.0, ""
    while True:
        try:
            report = mz.report(job_id)
            status = job_status(report)
            if status != last:
                log(f"job {job_id}: {status or '(no status yet)'}")
                last = status
            if status in TERMINAL:
                return report
        except MozarkError as e:
            if e.failure_class != LAB:
                raise
            log(f"poll: {e} (retrying)")
        if time.time() > deadline:
            return None
        sleep(delay)
        delay = min(delay * 1.5, 60)


def run(args: argparse.Namespace) -> int:
    results = Path(args.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    sha = os.environ.get("GITHUB_SHA", "local")
    run_id = os.environ.get("GITHUB_RUN_ID", str(int(time.time())))
    attempt_no = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    trigger = detect_trigger(os.environ.get("GITHUB_EVENT_NAME", ""), os.environ.get("GITHUB_REF", ""))
    meta: dict[str, Any] = {
        "platform": args.platform, "app_version": args.app_version, "commit": sha[:12], "trigger": trigger,
        "idempotency_key": f"{sha[:12]}-{run_id}-{attempt_no}",
        "ci_run_url": f"{server}/{repo}/actions/runs/{run_id}" if repo else "",
    }
    started = time.time()
    attempts: list[str] = []
    outcome = Outcome(TIMEOUT, "", UNCLASSIFIED, "did not start", [])

    try:
        if args.platform not in SUPPORTED_PLATFORMS:
            raise MozarkError(f"platform '{args.platform}' is not supported yet", PLATFORM)
        for name in ("token", "base_url", "org_id", "app_id"):
            if not getattr(args, name):
                raise MozarkError(f"{name.replace('_', '-')} is not set (GitHub secret or variable)", CI)
        check_host(args.base_url, args.allow_local)
        for extra in (args.runner_url, args.files_url, args.acp_url):
            if extra:
                check_host(extra, args.allow_local)

        config = load_config(Path(args.config))
        spec = resolve_spec(config, args.platform, trigger, {
            "project_id": args.project_id, "suite": args.suite, "devices": args.devices,
            "device_group": args.device_group, "timeout_minutes": args.timeout_minutes,
            "test_case_ids": args.test_case_ids})
        meta.update(suite_tags=",".join(spec.tags) or "(all)", devices=",".join(spec.devices) or spec.device_group)

        mz = Mozark(args, spec)
        upload = mz.get_build_file_id(Path(args.build_path))
        meta["build_file_id"] = upload.get("fileId")
        meta["app_upload_id"] = upload.get("id")
        log(f"build file ID {upload.get('fileId')} (upload {upload.get('id')}, {upload.get('packageName')})")
        ids = mz.resolve_test_case_ids()
        mz.lint()
        ci_meta = {k: meta[k] for k in ("app_version", "commit", "ci_run_url", "idempotency_key")}

        for retry in range(MAX_INFRA_RETRIES + 1):
            suite_name = f"ci-{args.platform}-{meta['idempotency_key']}-{retry}"
            try:
                sub = mz.submit(args.route, upload, ids, suite_name, args.scripts_zip, ci_meta)
                job_id = sub["job_id"]
                if not job_id:
                    raise MozarkError("submission returned no jobId", PLATFORM)
                meta.update(job_id=job_id, script_file_id=sub["script_file_id"], script_route=mz.route_used,
                            report_url=args.report_url_template.format(job_id=job_id, project_id=spec.project_id))
                set_output("job-id", job_id)
                report = poll(mz, job_id, spec.timeout_minutes)
                if report is None:
                    outcome = Outcome(TIMEOUT, "timeout", UNCLASSIFIED,
                                      f"no terminal status after {spec.timeout_minutes} min (job {job_id})", [])
                    attempts.append(f"{job_id}: timeout")
                    break
                (results / "report.json").write_text(json.dumps(report, indent=2))
                try:
                    (results / "tests.json").write_text(json.dumps(mz.tests(job_id), indent=2))
                except MozarkError as e:
                    log(f"tests: {e}")
                outcome = decide(report)
            except MozarkError as e:
                outcome = Outcome(INFRA if e.failure_class in INFRA_CLASSES else FAIL, "", e.failure_class, str(e), [])
            attempts.append(f"{meta.get('job_id', '-')}: exit {outcome.exit_code} {outcome.failure_class}".strip())
            if outcome.exit_code == INFRA and outcome.failure_class in RETRYABLE and retry < MAX_INFRA_RETRIES:
                log(f"infra failure ({outcome.failure_class}: {outcome.reason}); retry {retry + 1}/{MAX_INFRA_RETRIES}")
                time.sleep(args.retry_delay)
                continue
            break

        if args.fetch_artifacts and meta.get("job_id"):
            fetch_artifacts(mz, results)
    except MozarkError as e:
        code = INFRA if e.failure_class in INFRA_CLASSES else FAIL
        outcome = Outcome(code, "", e.failure_class, str(e), [])
        attempts.append(f"setup: exit {code} {e.failure_class}")

    meta["duration_s"] = int(time.time() - started)
    write_junit(results / "junit.xml", f"mozark-{args.platform}", outcome, meta)
    summary = write_summary(results / "summary.md", outcome, meta, attempts)
    (results / "meta.json").write_text(json.dumps({**meta, "exit_code": outcome.exit_code,
                                                   "failure_class": outcome.failure_class}, indent=2))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(summary)
    set_output("verdict", {PASS: "pass", FAIL: "fail", INFRA: "infra", TIMEOUT: "timeout"}[outcome.exit_code])
    set_output("report-url", meta.get("report_url", ""))
    set_output("failure-class", outcome.failure_class)
    log(f"exit {outcome.exit_code}: {outcome.reason}")
    return outcome.exit_code


def fetch_artifacts(mz: Mozark, results: Path) -> None:
    """Per-session JUnit from Optics. Off by default: see contract.md on the analytics host."""
    try:
        tests = json.loads((results / "tests.json").read_text())
    except (OSError, ValueError):
        return
    for row in (tests.get("data") or []) if isinstance(tests, dict) else []:
        tid = row.get("testId") if isinstance(row, dict) else None
        if not tid:
            continue
        try:
            xml = mz.session_junit(tid)
        except MozarkError as e:
            log(f"session {tid}: {e}")
            continue
        if xml:
            d = results / "sessions" / tid
            d.mkdir(parents=True, exist_ok=True)
            (d / "junit_output.xml").write_bytes(xml)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--build-path", default=env("INPUT_BUILD_PATH", ""))
    p.add_argument("--platform", default=env("INPUT_PLATFORM", "android"))
    p.add_argument("--project-id", default=env("INPUT_PROJECT_ID", ""))
    p.add_argument("--suite", default=env("INPUT_SUITE", ""), help="comma-separated Studio tags")
    p.add_argument("--test-case-ids", default=env("INPUT_TEST_CASE_IDS", ""))
    p.add_argument("--scripts-zip", default=env("INPUT_SCRIPTS_ZIP", ""))
    p.add_argument("--devices", default=env("INPUT_DEVICES", ""))
    p.add_argument("--device-group", default=env("INPUT_DEVICE_GROUP", ""))
    p.add_argument("--app-version", default=env("INPUT_APP_VERSION", ""))
    p.add_argument("--timeout-minutes", default=env("INPUT_TIMEOUT_MINUTES", ""))
    p.add_argument("--route", choices=("run-api", "package"), default=env("INPUT_ROUTE", "run-api"))
    p.add_argument("--config", default=env("INPUT_CONFIG", "mozark.yml"))
    p.add_argument("--results-dir", default=env("INPUT_RESULTS_DIR", "results"))
    p.add_argument("--report-url-template", default=env("INPUT_REPORT_URL_TEMPLATE",
                                                         "https://app-studio.preprod.mozark.ai/reports/{job_id}"))
    p.add_argument("--fetch-artifacts", action="store_true", default=env("INPUT_FETCH_ARTIFACTS", "false") == "true")
    p.add_argument("--retry-delay", type=float, default=float(env("MOZARK_RETRY_DELAY", "30")))
    p.add_argument("--allow-local", action="store_true", default=env("MOZARK_MOCK", "") == "1")
    a = p.parse_args(argv)
    # Secrets come from the environment only, never argv.
    a.token = env("MOZARK_TOKEN", "")
    a.base_url = env("MOZARK_BASE_URL", "")
    a.org_id = env("MOZARK_ORG_ID", "")
    a.app_id = env("MOZARK_APP_ID", "")
    a.runner_url = env("MOZARK_RUNNER_URL", "")
    a.files_url = env("MOZARK_FILES_URL", "")
    a.acp_url = env("MOZARK_ACP_URL", "")
    a.tenant = env("MOZARK_TENANT", "")
    return a


if __name__ == "__main__":
    sys.exit(run(parse_args()))
