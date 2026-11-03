# Mozark CI demo: test on every build

A prototype of **"test on every build"** with Mozark's real-device cloud. QA keep the test
scripts in a **Test Studio project**. This repo holds only the app and the **project ID**.
Every new build runs the project's latest scripts on real devices and returns a pass/fail,
JUnit, logs and a report link. Nobody opens the portal to start a run.

One Action works for every platform: Android phone, Android TV, iOS, LG webOS and Samsung Tizen.

```
QA edit scripts in Studio ──┐
                            ▼
git tag v1.2.0 ──► build app ──► mozark-run ──► upload build    → build file ID
                                            ├─► package scripts → script file ID (fresh every run)
                                            ├─► submit job with both IDs + devices
                                            ├─► poll to a verdict
                                            └─► junit.xml · summary · artifacts · exit code
```

More detail: [docs/journey.md](docs/journey.md).

## What's here

| Path | What it is |
|---|---|
| `.github/actions/mozark-run/` | The reusable Action: `action.yml`, `mozark_run.py`, a mock API for demos |
| `.github/workflows/build-and-test.yml` | On tag `v*`, release or manual run: build each platform, then test it on Mozark |
| `.github/workflows/ci.yml` | Unit tests, plus the pipeline run against the mock API for each verdict |
| `mozark.yml` | Studio project ID, tags per trigger, devices per platform |
| `apps/android/` | Tiny app (home → search → results → play). One APK for phone and Android TV |
| `apps/tv-web/`, `apps/webos/`, `apps/tizen/` | The same flow as a TV web app, packaged as `.ipk` and `.wgt` |
| `scripts/manual.sh` | The same flow by hand with curl, for debugging |

## Setup

1. **Secrets and variables** (Settings → Secrets and variables → Actions):

   | Name | Kind | Value |
   |---|---|---|
   | `MOZARK_TOKEN` | secret | Mozark API token |
   | `MOZARK_BASE_URL` | variable | App Studio API, e.g. `https://app-studio-be.preprod.mozark.ai` |
   | `MOZARK_ORG_ID` | variable | Mozark org ID |
   | `MOZARK_APP_ID` | variable | Mozark app ID |

   **Without `MOZARK_TOKEN` the Action runs in demo mode** against a built-in mock API, so the
   whole pipeline (build, run, verdict, JUnit, summary) can still be shown.

2. **`mozark.yml`**: set `project-id`, the devices (or `device-group`) per platform, and which
   Studio tags run on which trigger:

   ```yaml
   project-id: "<studio project id>"
   suites:
     tag: [smoke]
     release: [smoke, regression]
   platforms:
     android: { enabled: true, devices: ["<serial>"], timeout-minutes: 45 }
   ```

3. **Trigger**: push a tag (`git tag v0.1.0 && git push --tags`), publish a release, or run
   *Build and test on Mozark* by hand.

## Using the Action in your own repo

```yaml
- uses: mozarkai/mozark-run@v1          # this folder, once published
  with:
    build-path: app/build/outputs/apk/release/app-release.apk
    platform: android                   # android | android-tv | ios | webos | tizen
    suite: smoke                        # Studio tags; optional
    app-version: ${{ github.ref_name }}
    mozark-token: ${{ secrets.MOZARK_TOKEN }}
    mozark-base-url: ${{ vars.MOZARK_BASE_URL }}
    mozark-org-id: ${{ vars.MOZARK_ORG_ID }}
    mozark-app-id: ${{ vars.MOZARK_APP_ID }}
```

Outputs: `job-id`, `verdict` (`pass|fail|infra|timeout`), `report-url`, `failure-class`.

## Exit codes

| Code | Meaning | Retried? |
|---|---|---|
| `0` | All selected tests passed | — |
| `1` | A test failed (app, data, script) | Never |
| `2` | Infra error only (CI setup, platform/device, lab/connectivity) | Platform and lab: up to 2 times |
| `3` | Timeout or unknown state | No |

A job that is "completed" is not a pass: the verdict comes from each test's result.

## Failure classes

Each class needs a signal; nothing is forced into one. Rules live in `CLASS_RULES` in `mozark_run.py`.

| Class | Signal | Exit |
|---|---|---|
| `ci-integration` | HTTP 401/403, missing secret or config, request rejected (400), bad host | 2 |
| `platform-device` | "max queue size", "no free devices", device busy/offline, upload never ready, nothing ran | 2 (retried) |
| `lab-connectivity` | network errors, HTTP 5xx, "Force Aborted", controller lost | 2 (retried) |
| `automation-framework` | element not found, locator/XPath errors, lint errors | 1 |
| `data` | dataset/data-table errors, unresolved `${variable}` | 1 |
| `app` | crash, ANR, "has stopped", install failed | 1 |
| `unclassified` | a failure with none of the signals above | 1 |

## Adding a platform

1. Add it to `PLATFORMS` in `mozark_run.py` and to `mozark.yml` with `enabled: true` and devices.
2. Add a build step for it in `build-and-test.yml` that puts the build in `out/`.
3. If Studio or the device lab doesn't support it yet, the run stops early with
   "platform '…' is not supported yet" (exit 2), not a crash.

## Routes

- **`run-api`** (default): App Studio's `POST /jobs/:projectId/run` packages the project's
  latest saved scripts and submits the job in one call.
- **`package`**: the Action gets the script file ID itself (Studio export + codegen, or a
  manually exported zip as a last resort) and submits straight to the job runner with
  `testFramework: optics`. Use this to pick a device group (`trayId`).

## Assumptions in this prototype

- Optics is a job-runner and controller framework, and each session writes `junit_output.xml`.
  Until it's fetched (`fetch-artifacts: true`), `junit.xml` is generated from per-test results.
- Test Studio supports `android-tv`, `webos` and `tizen` as platforms.
- Test Studio tags select test cases (`GET /test-cases?tags=`, any-of match).
- CI uses a Mozark API token (today runs use a user's SSO token).

## Local checks

```bash
pip install -r .github/actions/mozark-run/requirements.txt
python -m unittest discover -s tests -v
```

Only `*.preprod.mozark.ai` and `*.dev.mozark.ai` hosts are accepted. Secrets come only from
GitHub Secrets or a local, gitignored `.env`.
