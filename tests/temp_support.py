"""Disposable test directories inside an explicitly scoped test root."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
from uuid import uuid4


@contextmanager
def temporary_directory():
    root = Path(os.environ.get("RESET_MONITOR_TEST_ROOT",
                str(Path(__file__).resolve().parents[1] / "runtime" / "test-tmp"))).resolve()
    directory = root / ("isolated-" + uuid4().hex)
    directory.mkdir(parents=True)
    try:
        yield str(directory)
    finally:
        resolved = directory.resolve()
        if resolved == root or not resolved.is_relative_to(root):
            raise AssertionError("UNSAFE_TEST_CLEANUP")
        shutil.rmtree(resolved)
