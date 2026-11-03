#!/usr/bin/env python3
"""A local stand-in for App Studio preprod, for demos and tests only.

Implements the route A calls with the response shapes read from App Studio code
(see contract.md). It never talks to Mozark.

  python mock_api.py --port 8765 --scenario pass|fail|flaky-infra|auth|data|app
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TEST_CASES = [
    {"id": "tc-search", "name": "Search returns results", "tags": ["smoke", "regression"]},
    {"id": "tc-play", "name": "Play starts from results", "tags": ["smoke", "regression"]},
    {"id": "tc-back", "name": "Back returns home", "tags": ["regression"]},
]

FAILURES = {
    "fail": ("Play starts from results", "Element not found: id=com.mozark.cidemo:id/play_button (locator)"),
    "data": ("Search returns results", "Unresolved variable ${search_term} in dataset ci_data"),
    "app": ("Play starts from results", "Application not responding: com.mozark.cidemo has stopped"),
}


class State:
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.jobs: dict[str, dict] = {}
        self.submissions = 0
        self.lock = threading.Lock()


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body):
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authed(self) -> bool:
            if state.scenario == "auth" or not self.headers.get("Authorization", "").startswith("Bearer "):
                self._send(401, {"message": "Unauthorized"})
                return False
            for h in ("x-org-id", "x-app-id", "x-project-id"):
                if not self.headers.get(h):
                    self._send(400, {"message": f"{h} header is required."})
                    return False
            return True

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_POST(self):
            if not self._authed():
                return
            body = self._body()
            path = self.path.split("?")[0]
            if path == "/app-uploads":
                name = "app.ipa" if b'filename="' in body and b'.ipa"' in body else "app.apk"
                return self._send(200, {"id": str(uuid.uuid4()), "fileId": uuid.uuid4().hex, "name": name,
                                        "platform": "ios" if name.endswith("ipa") else "android",
                                        "status": "ready", "packageName": "com.mozark.cidemo",
                                        "activity": ".MainActivity"})
            m = re.fullmatch(r"/jobs/([^/]+)/run", path)
            if m:
                req = json.loads(body or b"{}")
                if m.group(1) != self.headers.get("x-project-id"):
                    return self._send(403, {"message": "projectId does not match x-project-id"})
                with state.lock:
                    state.submissions += 1
                    job_id = uuid.uuid4().hex[:24]
                    ids = req.get("testCaseIds") or [t["id"] for t in TEST_CASES]
                    state.jobs[job_id] = {"polls": 0, "ids": ids, "devices": req.get("devices") or [],
                                          "submission": state.submissions}
                return self._send(202, {"jobId": job_id, "artifact": {"fileId": uuid.uuid4().hex}, "runJobIds": [job_id]})
            self._send(404, {"message": "not found"})

        def do_GET(self):
            if not self._authed():
                return
            path, _, query = self.path.partition("?")
            if path == "/test-cases":
                tags = set(re.search(r"tags=([^&]*)", query).group(1).split(",")) if "tags=" in query else set()
                rows = [t for t in TEST_CASES if not tags or tags & set(t["tags"])]
                return self._send(200, rows)
            if re.fullmatch(r"/optics-export/[^/]+/lint", path):
                return self._send(200, {"errors": []})
            m = re.fullmatch(r"/jobs/([^/]+)/(report|tests)", path)
            if m and m.group(1) in state.jobs:
                job = state.jobs[m.group(1)]
                if m.group(2) == "tests":
                    return self._send(200, {"data": [{"testId": f"s-{i}", "path": tid, "deviceSerial": d,
                                                      "testStatus": "Completed"}
                                                     for i, (tid, d) in enumerate((t, d) for t in job["ids"] for d in job["devices"])]})
                job["polls"] += 1
                return self._send(200, report_for(state.scenario, job))
            self._send(404, {"message": "not found"})

    return Handler


def report_for(scenario: str, job: dict) -> dict:
    polls = job["polls"]
    if polls == 1:
        return {"data": {"jobStatus": "Scheduled", "testReason": "Queued", "testCases": []}}
    if polls == 2:
        return {"data": {"jobStatus": "Started", "testCases": []}}
    names = {t["id"]: t["name"] for t in TEST_CASES}
    if scenario == "flaky-infra" and job["submission"] == 1:
        return {"data": {"jobStatus": "Failed", "testReason": "No free devices after 10 min",
                         "testCases": [{"type": "test case", "testCaseName": names[i], "deviceSerial": d,
                                        "testCaseStatus": "NOT YET STARTED"} for i in job["ids"] for d in job["devices"]]}}
    rows = []
    for i in job["ids"]:
        for d in job["devices"]:
            row = {"type": "test case", "testCaseName": names.get(i, i), "deviceSerial": d, "testCaseStatus": "PASS"}
            fail = FAILURES.get(scenario)
            if fail and names.get(i) == fail[0]:
                row.update(testCaseStatus="FAIL", reason=fail[1])
            rows.append(row)
    return {"data": {"jobStatus": "Completed", "testCases": rows}}


def serve(port: int, scenario: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(State(scenario)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--scenario", default="pass")
    a = ap.parse_args()
    print(f"mock App Studio on http://127.0.0.1:{a.port} scenario={a.scenario}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(State(a.scenario))).serve_forever()
