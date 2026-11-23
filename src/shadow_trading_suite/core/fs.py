"""Owner-only directory creation (files and directories readable by the current user only).

`Path.mkdir(parents=True, mode=0o700)` applies the mode only to the LAST level; every missing
parent is created with the process umask (typically 0775). `private_dirs` creates every
missing level 0700 instead. Directories that already exist are never re-moded: the caller's
existing layout (for example a shared checkout's `data/`) is left exactly as it is.

`private_umask` holds umask 0o077 while a command line entry point runs. Files that a
library creates lazily, after its own call returned, then start owner-only too: DuckDB writes
`<db>.wal` at the first write and again after every checkpoint, and `<db>.tmp/` when it spills;
child processes inherit the umask. A chmod afterwards would leave a window, and a umask scoped
to one call misses these files.
"""

import functools
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

PRIVATE_DIR_MODE = 0o700
PRIVATE_UMASK = 0o077


@contextmanager
def private_umask() -> Iterator[None]:
    """Umask 0o077 (never looser than the caller's) for the block; the previous umask after it."""
    previous = os.umask(PRIVATE_UMASK)
    os.umask(previous | PRIVATE_UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def owner_only_files[**P, R](main: Callable[P, R]) -> Callable[P, R]:
    """Run a command line `main()` under `private_umask` from its first line to its return."""

    @functools.wraps(main)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        with private_umask():
            return main(*args, **kwargs)

    return wrapper


def private_dirs(path: Path, *, exist_ok: bool = True) -> Path:
    """Create `path` and every missing parent with mode 0700 (subject to a stricter umask).

    With `exist_ok=False` an existing `path` raises FileExistsError, like `Path.mkdir`.
    """
    missing = [p for p in (path, *path.parents) if not p.exists()]
    if not missing and not exist_ok:
        raise FileExistsError(str(path))
    for p in reversed(missing):
        p.mkdir(mode=PRIVATE_DIR_MODE, exist_ok=p != path or exist_ok)
    return path
