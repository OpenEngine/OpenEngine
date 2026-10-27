from __future__ import annotations

from pathlib import Path

import pytest

from virtual_control_plane.exclusion import AlreadyRunning, StateDirectory


def test_a_second_process_cannot_take_an_already_held_lock(tmp_path: Path) -> None:
    first = StateDirectory(tmp_path)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            StateDirectory(tmp_path).acquire()
    finally:
        first.release()


def test_releasing_lets_another_process_take_it(tmp_path: Path) -> None:
    first = StateDirectory(tmp_path)
    first.acquire()
    first.release()

    second = StateDirectory(tmp_path)
    second.acquire()
    second.release()


def test_the_context_manager_releases_on_exit(tmp_path: Path) -> None:
    with StateDirectory(tmp_path):
        with pytest.raises(AlreadyRunning):
            StateDirectory(tmp_path).acquire()

    reacquired = StateDirectory(tmp_path)
    reacquired.acquire()
    reacquired.release()
