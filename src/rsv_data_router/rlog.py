"""Append-only call log: one line per tool call, file + stderr.

Never log Authorization values or tool arguments — only tool name, target
alias, outcome and duration. The log is the foundation of call auditing.
"""

import os
import sys
import threading
import time


class RouterLog:
    """Thread-safe line appender to <home>/router.log with stderr echo."""

    def __init__(self, home_dir, name="router.log", echo=True):
        self.path = os.path.join(home_dir, name)
        self.echo = echo
        self._lock = threading.Lock()

    def write(self, message):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}"
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass  # logging must never break the router
            if self.echo:
                print(line, file=sys.stderr, flush=True)
