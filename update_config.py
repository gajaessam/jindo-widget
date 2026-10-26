"""Release settings shared by the widget and the release build script."""

APP_VERSION = "0.2.4"
APP_FOLDER_NAME = "JindoWidget"

# Public GitHub repository in OWNER/REPOSITORY form.
GITHUB_REPOSITORY = "gajaessam/jindo-widget"

RELEASE_ASSET_TEMPLATE = "JindoWidget-Setup-v{version}.exe"
# Kept only so the unused legacy updater.py remains readable; new builds do not package it.
UPDATER_EXECUTABLE_NAME = "UsagiJindoUpdater.exe"
MAX_UPDATE_BYTES = 512 * 1024 * 1024
