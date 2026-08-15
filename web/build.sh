#!/bin/sh -eu
# Assemble the static site into $1 (default: site/), stamping the current version.
#
# Called by both publishers: .github/workflows/site.yml for copy-only edits, and
# release.yml, which then drops the signed ostree repo in beside it. The version
# is read from meson.build so the page can never drift from the release.
#
# Local preview:  sh web/build.sh /tmp/site && python3 -m http.server -d /tmp/site

out=${1:-site}
version=$(grep -m1 "version:" meson.build | sed -E "s/.*'([^']+)'.*/\1/")

# Copied file by file, not `cp -r web/img`, which nests into img/img on a rerun.
mkdir -p "$out/img"
cp web/index.html web/robots.txt web/sitemap.xml "$out/"
cp web/img/* "$out/img/"
cp data/icons/hicolor/256x256/apps/in.gxanshu.postcard.png "$out/icon.png"
sed -i "s/__VERSION__/$version/g" "$out/index.html"

echo "built $out at version $version"
