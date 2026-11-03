#!/usr/bin/env bash
# Phase 1, route A: run an App Studio project against a new build, by hand.
#
#   scripts/manual.sh <path/to/app.apk|.ipa>
#
# Reads .env (see .env.example). Steps:
#   1. POST /app-uploads                   build -> appUploadId (+ files-api fileId)
#   2. GET  /optics-export/:projectId/lint must be clean
#   3. POST /jobs/:projectId/run           App Studio packages the latest saved scripts,
#                                          uploads them (script fileId) and books the job
#   4. GET  /jobs/:jobId/report            poll to a terminal status
#   5. GET  /jobs/:jobId/tests             per-session results
# Session files (/sessions/...) are NOT fetched: App Studio preprod reads them from a
# host outside *.preprod.mozark.ai (LEGACY_ANALYTICS_API_URL), pending a decision.
#
# Output goes to results/<timestamp>/ (gitignored). The token is never printed.
set -euo pipefail

BUILD_PATH="${1:?usage: scripts/manual.sh <path/to/app.apk|.ipa>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[[ -f "$ROOT/.env" ]] || { echo "Missing $ROOT/.env (copy .env.example)" >&2; exit 2; }
set -a; source "$ROOT/.env"; set +a

for v in MOZARK_BASE_URL MOZARK_TOKEN MOZARK_ORG_ID MOZARK_APP_ID MOZARK_PROJECT_ID MOZARK_PLATFORM MOZARK_DEVICES; do
  [[ -n "${!v:-}" ]] || { echo "$v is not set in .env" >&2; exit 2; }
done
[[ -f "$BUILD_PATH" ]] || { echo "No build at $BUILD_PATH" >&2; exit 2; }

# Hard rule: preprod or dev only.
host="$(sed -E 's#^https://([^/:]+).*#\1#' <<<"$MOZARK_BASE_URL")"
if [[ ! "$MOZARK_BASE_URL" =~ ^https:// ]] || [[ ! "$host" =~ \.(preprod|dev)\.mozark\.ai$ ]]; then
  echo "Refusing host '$host': only *.preprod.mozark.ai or *.dev.mozark.ai" >&2; exit 2
fi
BASE="${MOZARK_BASE_URL%/}"
PROJECT="$MOZARK_PROJECT_ID"
TIMEOUT_MIN="${MOZARK_TIMEOUT_MINUTES:-45}"

OUT="$ROOT/results/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$OUT"
LOG="$OUT/timeline.log"
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { echo "$(ts) $*" | tee -a "$LOG"; }

# Headers in a private temp file so the token never appears in argv or logs.
HDR="$(mktemp)"; chmod 600 "$HDR"; trap 'rm -f "$HDR"' EXIT
{
  echo "Authorization: Bearer $MOZARK_TOKEN"
  echo "x-org-id: $MOZARK_ORG_ID"
  echo "x-app-id: $MOZARK_APP_ID"
  echo "x-project-id: $PROJECT"
  echo "Accept: application/json"
} > "$HDR"

# api METHOD PATH OUTFILE [curl args...] -> prints HTTP status; body in OUTFILE.
api() {
  local method="$1" path="$2" out="$3"; shift 3
  curl -sS -X "$method" -H @"$HDR" -o "$out" -w '%{http_code}' "$@" "$BASE$path"
}
need_2xx() {
  local code="$1" what="$2" body="$3"
  if [[ "$code" != 2* ]]; then
    say "FAIL $what: HTTP $code"; jq . "$body" 2>/dev/null || cat "$body"; exit 1
  fi
}

say "base=$BASE project=$PROJECT platform=$MOZARK_PLATFORM devices=$MOZARK_DEVICES build=$(basename "$BUILD_PATH")"

# 1. Build upload
say "1/5 upload build"
code=$(api POST /app-uploads "$OUT/upload.json" -F "file=@$BUILD_PATH")
need_2xx "$code" "upload" "$OUT/upload.json"
UPLOAD_ID=$(jq -r '.id' "$OUT/upload.json")
say "    appUploadId=$UPLOAD_ID buildFileId=$(jq -r '.fileId' "$OUT/upload.json") status=$(jq -r '.status' "$OUT/upload.json") package=$(jq -r '.packageName' "$OUT/upload.json")"

# iOS builds are resigned by files-api first.
deadline=$(( $(date +%s) + 900 ))
while [[ "$(jq -r '.status' "$OUT/upload.json")" == "processing" ]]; do
  (( $(date +%s) < deadline )) || { say "FAIL build still processing after 15 min"; exit 3; }
  sleep 15
  code=$(api GET "/app-uploads/$UPLOAD_ID/status" "$OUT/upload.json")
  need_2xx "$code" "upload status" "$OUT/upload.json"
  say "    upload status=$(jq -r '.status' "$OUT/upload.json")"
done
[[ "$(jq -r '.status' "$OUT/upload.json")" == "ready" ]] || { say "FAIL build status $(jq -r '.status' "$OUT/upload.json")"; exit 1; }

# 2. Lint
say "2/5 lint project"
code=$(api GET "/optics-export/$PROJECT/lint" "$OUT/lint.json")
need_2xx "$code" "lint" "$OUT/lint.json"
n=$(jq '.errors | length' "$OUT/lint.json")
[[ "$n" == 0 ]] || { say "FAIL lint: $n error(s)"; jq '.errors' "$OUT/lint.json"; exit 1; }

# 3. Run
say "3/5 submit run"
SUITE="ci-demo-${MOZARK_PLATFORM}-$(date -u +%Y%m%d%H%M%S)"
jq -n \
  --arg platform "$MOZARK_PLATFORM" \
  --arg devices "$MOZARK_DEVICES" \
  --arg upload "$UPLOAD_ID" \
  --arg suite "$SUITE" \
  --arg ids "${MOZARK_TEST_CASE_IDS:-}" \
  --argjson maxTest "${MOZARK_MAX_TEST_MINUTES:-10}" \
  '{platform: $platform,
    devices: ($devices | split(",") | map(select(length > 0))),
    appUploadId: $upload,
    installApp: true,
    mode: "full",
    maxTestDuration: $maxTest,
    suiteName: $suite}
   + (if $ids == "" then {} else {testCaseIds: ($ids | split(","))} end)' > "$OUT/run-request.json"
code=$(api POST "/jobs/$PROJECT/run" "$OUT/run.json" -H 'Content-Type: application/json' --data @"$OUT/run-request.json")
need_2xx "$code" "run" "$OUT/run.json"
JOB_ID=$(jq -r '.jobId' "$OUT/run.json")
say "    jobId=$JOB_ID scriptFileId=$(jq -r '.artifact.fileId' "$OUT/run.json") suite=$SUITE"

# 4. Poll
say "4/5 poll (timeout ${TIMEOUT_MIN} min)"
deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
delay=10; last=""
while :; do
  code=$(api GET "/jobs/$JOB_ID/report" "$OUT/report.json")
  if [[ "$code" == 2* ]]; then
    status=$(jq -r '.data.jobStatus // "unknown" | ascii_downcase' "$OUT/report.json")
    [[ "$status" != "$last" ]] && say "    status=$status" && last="$status"
    case "$status" in completed|passed|failed|error|aborted|"force aborted") break ;; esac
  else
    say "    report HTTP $code (will retry)"
  fi
  (( $(date +%s) < deadline )) || { say "TIMEOUT after ${TIMEOUT_MIN} min, last status=$last"; exit 3; }
  sleep "$delay"; (( delay < 60 )) && delay=$(( delay * 2 > 60 ? 60 : delay * 2 ))
done

# 5. Results
say "5/5 fetch tests"
code=$(api GET "/jobs/$JOB_ID/tests" "$OUT/tests.json")
[[ "$code" == 2* ]] || say "    tests HTTP $code"
jq -r '.data.testCases // [] | .[] | "    \(.testStatus // .status // "?")\t\(.testCaseName // .name // .path // "?")"' "$OUT/report.json" | tee -a "$LOG" || true
say "done: job=$JOB_ID status=$last results=$OUT"
