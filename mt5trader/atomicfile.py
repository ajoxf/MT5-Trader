"""One way to replace a file, and it has to work on Windows.

Every file this system publishes — the status snapshot, the command
results, the config — is written to a tmp file and then renamed over
the destination, so a reader never catches half a document.

On Windows that rename FAILS while any other process has the
destination open:

    PermissionError: [WinError 5] Access is denied:
        'status.json.tmp' -> 'status.json'

The web process reads `status.json` several times a second, so the
collision is not rare — it is the normal case under load, and when it
hits, the coordinator's whole poll fails and the screen goes stale
while the market moves. (POSIX has no such rule: a rename over an open
file is always allowed there, which is why this only ever shows up on
the trading box.)

The fix is to WAIT and try again. A reader holds the file for a
fraction of a millisecond, so a few short retries clear every ordinary
collision. What is not acceptable is either alternative:

- writing straight over the destination, which is exactly the
  half-read this tmp-file dance exists to prevent;
- giving up and letting the poll fail, which is what was happening.

`replace()` returns True when the file was replaced and False when it
could not be, after trying — the caller decides whether that is worth
saying out loud. It never raises on the collision it is here for.
"""

import errno
import logging
import os
import time

#: About a fifth of a second of retries. A reader holds the file for
#: microseconds; anything that outlasts this is not a collision — it is
#: an antivirus scanner, a backup agent, or a file that has been made
#: read-only, and none of those is fixed by waiting longer.
ATTEMPTS = 20
DELAY_SEC = 0.01


def replace(tmp, path, attempts=ATTEMPTS, delay=DELAY_SEC, sleep=time.sleep):
    """`os.replace(tmp, path)`, retried through a Windows reader."""
    last = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError as e:
            # WinError 5 (access denied) and WinError 32 (in use) are
            # both "someone has it open"; a genuine permissions problem
            # raises the same code and simply keeps failing, which the
            # caller then reports.
            last = e
            if attempt + 1 < attempts:
                sleep(delay)
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EBUSY):
                raise
            last = e
            if attempt + 1 < attempts:
                sleep(delay)
    logging.error('could not replace %s (%s). The file on disk is the '
                  'PREVIOUS one — nothing was half-written.', path, last)
    return False


def write_json(path, payload, dumps=None):
    """Write a JSON document atomically, or leave the old one alone."""
    import json
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write((dumps or json.dumps)(payload))
    if not replace(tmp, path):
        # The tmp file is left where it is: the next pass overwrites it,
        # and a half-written destination is never the alternative.
        return False
    return True
