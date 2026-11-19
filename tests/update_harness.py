"""Runs a TEMP PROJECT's dashboard/server.py with one protection switched off, for the update tests.

Reads GOW_UPDATE_HARNESS (JSON). Only tests/test_dashboard_update.py starts this.
  project                    the temp project's folder (never the real one)
  fetch_timeout              seconds: updater.FETCH_TIMEOUT_SECONDS
  skip_overwrite_guard       G1: the would-replace guard says nothing would be replaced
  drop_no_overwrite_ignore   G1: git's own --no-overwrite-ignore is taken off the merge
  skip_startup_check         G2: the startup check always passes
  exec_fails                 D3: os.execv raises PermissionError
"""

import json
import os
import sys

config = json.loads(os.environ["GOW_UPDATE_HARNESS"])
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REAL_PROJECT = os.path.dirname(TESTS_DIR)
project = os.path.realpath(config["project"])
if project == os.path.realpath(REAL_PROJECT) or project.startswith(os.path.realpath(REAL_PROJECT) + os.sep):
    sys.exit("Refusing to run: the project is the real checkout")

sys.dont_write_bytecode = True
sys.path.insert(0, project)

from dashboard import server, updater  # noqa: E402

if os.path.realpath(updater.BASE_DIR) != project:
    sys.exit("Refusing to run: the updater is not the temp project's")

if "fetch_timeout" in config:
    updater.FETCH_TIMEOUT_SECONDS = config["fetch_timeout"]
if config.get("skip_overwrite_guard"):
    updater.would_replace = lambda path, *args, **kwargs: False
if config.get("skip_startup_check"):
    updater.passes_startup_check = lambda: True
if config.get("drop_no_overwrite_ignore"):
    real_git = updater.git

    def git(arguments, timeout=None):
        if arguments[:1] == ["merge"]:
            arguments = [a for a in arguments if a != "--no-overwrite-ignore"]
        return real_git(arguments, timeout)

    updater.git = git
if config.get("exec_fails"):
    def execv(path, arguments):
        raise PermissionError("blocked by the test harness")

    os.execv = execv

sys.argv = [os.path.join(project, "dashboard", "server.py")] + sys.argv[1:]
server.main()
