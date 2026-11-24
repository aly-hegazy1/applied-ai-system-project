"""One place to configure logging for the app, the CLI and the evaluator."""

import logging
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "coach.log"

_configured = False


def configure_logging(level: int = logging.INFO, console: bool = False) -> Path:
    """Send `coach.*` logs to logs/coach.log (and optionally the console).

    Safe to call repeatedly - Streamlit reruns the script on every click, and
    adding a new handler each time would duplicate every log line.
    """
    global _configured
    root = logging.getLogger("coach")
    root.setLevel(level)
    if _configured:
        return LOG_FILE

    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("  [log] %(levelname)s %(message)s"))
        root.addHandler(stream)
    _configured = True
    return LOG_FILE
