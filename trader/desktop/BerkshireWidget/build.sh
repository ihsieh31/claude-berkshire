#!/bin/sh
# 編譯桌面小儀表板 → build/BerkshireWidget.app（只需 Command Line Tools，不需 Xcode）
set -eu
cd "$(dirname "$0")"
APP=build/BerkshireWidget.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
swiftc -O -swift-version 5 -target arm64-apple-macos13.0 main.swift -o "$APP/Contents/MacOS/BerkshireWidget"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>local.berkshire.widget</string>
  <key>CFBundleName</key><string>BerkshireWidget</string>
  <key>CFBundleExecutable</key><string>BerkshireWidget</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict></plist>
PLIST
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
echo "$APP"
