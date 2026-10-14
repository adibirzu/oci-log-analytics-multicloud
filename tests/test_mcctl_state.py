import threading
import time

import pytest

from mcctl.state import Layout, LockBusy, Manifest, file_lock, mcctl_home, read_manifest, write_manifest


def test_layout_paths_and_backend_key(tmp_path):
    lay = Layout(tmp_path, "aws", "123456789012", "logs")
    assert lay.backend_key == "state/aws/123456789012/logs.tfstate"
    assert lay.workdir == tmp_path / "work/aws/123456789012/logs"


def test_layout_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        Layout(tmp_path, "aws", "../evil", "logs")


def test_home_override(tmp_path):
    assert mcctl_home({"MCCTL_HOME": str(tmp_path)}) == tmp_path


def test_manifest_roundtrip(tmp_path):
    lay = Layout(tmp_path, "gcp", "proj-x", "logs")
    assert read_manifest(lay) is None
    m = Manifest("p", "abc", 7, {"add": 1})
    write_manifest(lay, m)
    assert read_manifest(lay) == m


def test_lock_excludes_concurrent_holders(tmp_path):
    path = tmp_path / "l" / "x.lock"
    held, release, results = threading.Event(), threading.Event(), []

    def holder():
        with file_lock(path):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(5)
    with pytest.raises(LockBusy):
        with file_lock(path):
            results.append("entered")
    release.set()
    t.join()
    with file_lock(path):  # free again
        results.append("reacquired")
    assert results == ["reacquired"]


def test_lock_waits_within_timeout(tmp_path):
    path = tmp_path / "x.lock"
    order = []

    def first():
        with file_lock(path):
            order.append("a")
            time.sleep(0.2)

    t = threading.Thread(target=first)
    t.start()
    time.sleep(0.05)
    with file_lock(path, timeout=3):
        order.append("b")
    t.join()
    assert order == ["a", "b"]
