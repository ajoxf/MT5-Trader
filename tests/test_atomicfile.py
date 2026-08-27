"""Replacing a file, on the platform where that is not free.

Every file this system publishes goes through a tmp file and a rename,
so a reader never catches half a document. On Windows that rename fails
outright while any other process has the destination open — and the web
process reads the status snapshot several times a second. Unretried,
the collision failed the whole poll and froze the screen with the
market moving.
"""

import json
import os

import pytest

from mt5trader import atomicfile


def test_a_collision_is_waited_out_rather_than_raised(tmp_path):
    """The Windows case, modelled: the first attempts are refused, one
    later attempt succeeds, and the caller never sees an exception."""
    target = tmp_path / 'status.json'
    target.write_text('old', encoding='utf-8')
    source = tmp_path / 'status.json.tmp'
    source.write_text('new', encoding='utf-8')

    attempts = {'n': 0}
    real = os.replace

    def flaky(src, dst):
        attempts['n'] += 1
        if attempts['n'] < 3:
            raise PermissionError(5, 'Access is denied')
        return real(src, dst)

    original, atomicfile.os.replace = atomicfile.os.replace, flaky
    try:
        ok = atomicfile.replace(str(source), str(target), delay=0.0)
    finally:
        atomicfile.os.replace = original

    assert ok is True
    assert attempts['n'] == 3
    assert target.read_text(encoding='utf-8') == 'new'


def test_a_file_that_never_frees_up_leaves_the_OLD_one_intact(tmp_path):
    """Anything outlasting the retries is not a collision — it is a
    scanner, a backup agent or a read-only file. The destination then
    keeps the PREVIOUS document: a half-written one is never the
    alternative."""
    target = tmp_path / 'status.json'
    target.write_text('old', encoding='utf-8')
    source = tmp_path / 'status.json.tmp'
    source.write_text('new', encoding='utf-8')

    def always_denied(src, dst):
        raise PermissionError(5, 'Access is denied')

    original, atomicfile.os.replace = atomicfile.os.replace, always_denied
    try:
        ok = atomicfile.replace(str(source), str(target), attempts=3,
                                delay=0.0)
    finally:
        atomicfile.os.replace = original

    assert ok is False
    assert target.read_text(encoding='utf-8') == 'old'
    assert source.exists()          # the next pass overwrites it


def test_a_real_permissions_fault_is_not_mistaken_for_a_collision(tmp_path):
    """It still comes back False and is logged — but the point of the
    test is that nothing is written to the destination in the attempt."""
    target = tmp_path / 'status.json'
    source = tmp_path / 'status.json.tmp'
    source.write_text('new', encoding='utf-8')

    def always_denied(src, dst):
        raise PermissionError(13, 'Permission denied')

    original, atomicfile.os.replace = atomicfile.os.replace, always_denied
    try:
        assert atomicfile.replace(str(source), str(target), attempts=2,
                                  delay=0.0) is False
    finally:
        atomicfile.os.replace = original
    assert not target.exists()


def test_the_engine_keeps_publishing_through_a_locked_reader(config, pair,
                                                              legs, tmp_path):
    """End to end: a poll whose replace is refused does not fail the
    poll. The screen shows the previous snapshot and its age, which is
    what the staleness banner is for."""
    from mt5trader.coordinator import Coordinator
    status = tmp_path / 'status.json'
    coordinator = Coordinator(config, legs, status_path=str(status))
    coordinator.start()
    coordinator.poll_once()
    coordinator.publish()
    first = json.loads(status.read_text(encoding='utf-8'))

    def always_denied(src, dst):
        raise PermissionError(5, 'Access is denied')

    original, atomicfile.os.replace = atomicfile.os.replace, always_denied
    try:
        coordinator.poll_once()
        coordinator.publish()            # must not raise
    finally:
        atomicfile.os.replace = original

    assert json.loads(status.read_text(encoding='utf-8'))['at'] == first['at']
    coordinator.publish()                # and it recovers by itself
    assert json.loads(status.read_text(encoding='utf-8'))['at'] > first['at']
