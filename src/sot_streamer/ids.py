"""Small dependency-free ULID generator for utterance correlation IDs."""

from __future__ import annotations

import secrets
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LOCK = threading.Lock()
_LAST_MS = -1
_LAST_RANDOM = 0


def new_ulid() -> str:
    global _LAST_MS, _LAST_RANDOM
    now_ms = int(time.time() * 1_000)
    with _LOCK:
        if now_ms == _LAST_MS:
            _LAST_RANDOM = (_LAST_RANDOM + 1) & ((1 << 80) - 1)
        else:
            _LAST_MS = now_ms
            _LAST_RANDOM = secrets.randbits(80)
        value = (now_ms << 80) | _LAST_RANDOM
    encoded = ["0"] * 26
    for index in range(25, -1, -1):
        encoded[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(encoded)
