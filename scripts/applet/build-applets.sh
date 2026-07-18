#!/bin/bash
# build-applets.sh — (re)generate the double-clickable Mac apps for KeepBook.
#
# Produces, next to this script:
#   KeepBook.app        -> runs start-keepbook.command  (with the product icon)
#   Stop KeepBook.app   -> runs stop-keepbook.command
#
# The .app bundles are osacompile output (machine-generated Mach-O + resources)
# and are NOT committed — run this script once on the machine to (re)create
# them, then drag KeepBook.app to the Dock. Icon is generated from
# frontend/assets/logo-mark.svg via rsvg-convert + iconutil.
#
# Requires: rsvg-convert (brew install librsvg), iconutil, osacompile (macOS).

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SVG="$REPO_ROOT/frontend/assets/logo-mark.svg"
RUN_DIR="$SCRIPT_DIR/.run"
mkdir -p "$RUN_DIR"

RSVG="$(command -v rsvg-convert || true)"
if [ -z "$RSVG" ]; then
  echo "ERROR: rsvg-convert not found (brew install librsvg). Cannot build icon." >&2
  exit 1
fi

# --- 1. Build the .icns from the SVG ----------------------------------------
ICNS="$RUN_DIR/KeepBook.icns"
if [ -f "$SVG" ]; then
  echo "Building icon from $SVG ..."
  ICONSET="$RUN_DIR/KeepBook.iconset"
  rm -rf "$ICONSET"; mkdir -p "$ICONSET"
  # name:size pairs for a full macOS iconset
  for spec in \
    icon_16x16.png:16 icon_16x16@2x.png:32 \
    icon_32x32.png:32 icon_32x32@2x.png:64 \
    icon_128x128.png:128 icon_128x128@2x.png:256 \
    icon_256x256.png:256 icon_256x256@2x.png:512 \
    icon_512x512.png:512 icon_512x512@2x.png:1024; do
    name="${spec%%:*}"; size="${spec##*:}"
    "$RSVG" -w "$size" -h "$size" "$SVG" -o "$ICONSET/$name"
  done
  /usr/bin/iconutil -c icns "$ICONSET" -o "$ICNS"
  echo "  wrote $ICNS"
else
  echo "WARNING: $SVG not found — apps will use the default AppleScript icon." >&2
  ICNS=""
fi

# --- 2. Compile the two apps -------------------------------------------------
build_app() {
  app_name="$1"; command_file="$2"; stay_open="${3:-}"
  app_path="$SCRIPT_DIR/$app_name"
  cmd_path="$SCRIPT_DIR/$command_file"
  echo "Compiling $app_name -> runs $command_file ..."
  rm -rf "$app_path"
  if [ "$stay_open" = "stay" ]; then
    # STAY-OPEN applet: launches the stack, then remains in the Dock with the
    # running indicator while KeepBook is up — the "this is real software"
    # affordance. Quitting the icon deliberately does NOT stop the server
    # (that's stop-keepbook.command's job); it just removes the Dock presence.
    src="$RUN_DIR/keepbook-applet.applescript"
    cat > "$src" <<APPLESCRIPT
on run
  do shell script "/bin/bash " & quoted form of "$cmd_path"
end run
on idle
  return 3600
end idle
APPLESCRIPT
    /usr/bin/osacompile -s -o "$app_path" "$src"
  else
    # run-and-exit wrapper (Stop): do shell script via bash, absolute path.
    /usr/bin/osacompile -o "$app_path" \
      -e "do shell script \"/bin/bash \" & quoted form of \"$cmd_path\""
  fi

  if [ -n "$ICNS" ]; then
    res_dir="$app_path/Contents/Resources"
    plist="$app_path/Contents/Info.plist"
    # Overwrite the .icns the bundle references (osacompile names it
    # applet.icns and points CFBundleIconFile at "applet").
    existing_icns="$(/bin/ls "$res_dir"/*.icns 2>/dev/null | head -1 || true)"
    if [ -n "$existing_icns" ]; then
      /bin/cp "$ICNS" "$existing_icns"
      echo "  icon -> $(basename "$existing_icns")"
    else
      /bin/cp "$ICNS" "$res_dir/applet.icns"
      echo "  icon -> applet.icns (created)"
    fi
    # Modern osacompile ALSO ships the default icon inside Assets.car and
    # points CFBundleIconName at it — the asset catalog wins over applet.icns,
    # so our swap would be invisible. Force the legacy path: drop the asset
    # catalog + the CFBundleIconName key, keep CFBundleIconFile -> applet.icns.
    [ -f "$res_dir/Assets.car" ] && rm -f "$res_dir/Assets.car" && echo "  removed Assets.car (default icon override)"
    /usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$plist" >/dev/null 2>&1 || true
    /usr/libexec/PlistBuddy -c "Set :CFBundleIconFile applet" "$plist" >/dev/null 2>&1 \
      || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string applet" "$plist" >/dev/null 2>&1 || true
    # Editing the bundle invalidated the ad-hoc signature osacompile applied;
    # re-sign ad-hoc so the app launches cleanly.
    /usr/bin/codesign --force --sign - "$app_path" >/dev/null 2>&1 \
      && echo "  re-signed ad-hoc" || echo "  (codesign re-sign skipped)"
    # Bust Finder/Dock icon caches for this bundle.
    /usr/bin/touch "$app_path"
  fi
}

build_app "KeepBook.app" "start-keepbook.command" "stay"
build_app "Stop KeepBook.app" "stop-keepbook.command"

echo
echo "Done. Built:"
echo "  $SCRIPT_DIR/KeepBook.app"
echo "  $SCRIPT_DIR/Stop KeepBook.app"
echo "Drag KeepBook.app to the Dock to launch KeepBook with one click."
