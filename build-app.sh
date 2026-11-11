#!/bin/bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
app_dir="$project_dir/build/Claude Desktop Companion.app"
if [[ -d "$app_dir" ]]; then /bin/rm -rf -- "$app_dir"; fi
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
/usr/bin/xcrun swiftc -swift-version 5 -O -framework AppKit \
  "$project_dir"/Sources/*.swift \
  -o "$app_dir/Contents/MacOS/ClaudeDesktopCompanion"
/bin/cp "$project_dir/Info.plist" "$app_dir/Contents/Info.plist"
# Copy only application code/documentation; never copy local secrets or backups.
for source in "$project_dir"/backend/*.py; do
  case "$(basename "$source")" in test_*) continue ;; esac
  /bin/cp "$source" "$app_dir/Contents/Resources/"
done
for document in README.md README.zh-CN.md PRIVACY.md SECURITY.md LICENSE; do
  if [[ -f "$project_dir/$document" ]]; then
    /bin/cp "$project_dir/$document" "$app_dir/Contents/Resources/$document"
  fi
done
/usr/bin/plutil -lint "$app_dir/Contents/Info.plist"
"$app_dir/Contents/MacOS/ClaudeDesktopCompanion" --self-test
/usr/bin/codesign --force --deep --sign - "$app_dir"
/usr/bin/codesign --verify --deep --strict "$app_dir"
printf '%s\n' "$app_dir"
