"""Last-resort error reporting. A windowed (no-console) build has nowhere to print a traceback,
so unexpected exceptions are written to crash.log in the data folder and shown in a dialog."""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional


def install(folder: Path, show: Optional[Callable[[str, str], None]] = None) -> None:
    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "crash.log").open("a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
        except OSError:
            pass
        if sys.__stderr__ is not None:
            sys.__excepthook__(exc_type, exc, tb)
        if show is not None and threading.current_thread() is threading.main_thread():
            show("Unexpected error", f"{exc_type.__name__}: {exc}\n\nDetails were written to {folder / 'crash.log'}")

    sys.excepthook = hook
