#!/bin/bash
# Build macOS : genere dist/Classeur1.app (a lancer sur une machine Mac)
set -e
cd "$(dirname "$0")"

pip install --quiet pyinstaller pillow

pyinstaller --onefile --windowed \
    --icon=icon.icns \
    --add-data "custom_track.mp3:." \
    --name Classeur1 \
    automation_tool.py

echo
echo "Termine. App dans dist/Classeur1.app"
