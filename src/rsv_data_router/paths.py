"""Runtime file locations: config home (targets.json) and log directory.

Defaults follow platformdirs: the user config directory for targets.json
(Windows: %LOCALAPPDATA%\\rsv-data-router, Linux: ~/.config/rsv-data-router)
and the user log directory for router.log (…\\Logs next to it on Windows).
--home / RSV_DATA_ROUTER_HOME override both to a single directory — tests
and portable runs rely on that.
"""

import os

from platformdirs import user_config_dir, user_log_dir

APP_NAME = "rsv-data-router"


def default_home():
    """Directory for targets.json, created on demand."""
    home = user_config_dir(APP_NAME, appauthor=False)
    os.makedirs(home, exist_ok=True)
    return home


def default_log_dir():
    """Directory for router.log when the home is not overridden."""
    log_dir = user_log_dir(APP_NAME, appauthor=False)
    os.makedirs(log_dir, exist_ok=True)
    return log_dir
