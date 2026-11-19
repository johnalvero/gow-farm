#!/bin/bash
# Double-click in Finder to update this project from GitHub, then restart the
# always-on dashboard so it runs the new code. Never installs Python packages.

# The braces make bash read the whole script before running it, so the
# pull can safely replace this file
{
    finish() {
        echo
        read -r -p "Press Enter to close this window. " _
        exit "$1"
    }

    cd "$(dirname "$0")" || finish 1
    echo "Updating $(pwd)"
    echo

    if [ ! -d .git ]; then
        echo "This folder isn't connected to GitHub yet, so it can't update."
        echo "See \"Updating from GitHub\" in README.md for the one-time setup."
        finish 1
    fi

    # --ff-only never makes a merge, so a folder with local edits stops here
    # instead of ending up half-merged
    if ! git pull --ff-only; then
        echo
        echo "The update didn't go through, and nothing was changed."
        echo "If it mentions local changes, a project file was edited on this Mac."
        finish 1
    fi

    echo
    if [ -f "$HOME/Library/LaunchAgents/com.godofwar.dashboard.plist" ]; then
        echo "Restarting the dashboard so it uses the new code..."
        if ! bash dashboard/service.sh install; then
            echo
            echo "The code is updated, but the dashboard didn't restart."
            echo "If a claim run is going, double-click this again after it finishes."
            finish 1
        fi
    else
        echo "The always-on dashboard isn't installed here, so there's nothing to restart."
    fi

    echo
    echo "Update finished."
    finish 0
}
