#!/usr/bin/env bash
# Package the shared TV web app for webOS (.ipk) or Tizen (.wgt).
#   scripts/package-tv-web.sh webos|tizen <version> <out-dir>
# webOS uses the official ares-package (npm @webos-tools/cli).
# Tizen: an unsigned .wgt zip. A real install needs `tizen package -t wgt -s <profile>`
# with a Samsung certificate; that is out of scope for the demo.
set -euo pipefail
platform="$1"; version="${2#v}"; out="$3"
root="$(cd "$(dirname "$0")/.." && pwd)"
stage="$(mktemp -d)"
cp "$root/apps/tv-web/index.html" "$root/apps/tv-web/icon.png" "$stage/"
mkdir -p "$out"
case "$platform" in
  webos)
    jq --arg v "$version" '.version = $v' "$root/apps/webos/appinfo.json" > "$stage/appinfo.json"
    npx --yes --package @webos-tools/cli@3 ares-package "$stage" --outdir "$out" --no-minify
    ;;
  tizen)
    sed -E "s/version=\"[0-9.]+\" viewmodes/version=\"$version\" viewmodes/" "$root/apps/tizen/config.xml" > "$stage/config.xml"
    (cd "$stage" && zip -qr "$out/cidemo-$version.wgt" .)
    ;;
  *) echo "unknown platform $platform" >&2; exit 2 ;;
esac
ls -l "$out"
