"""The part store: exported meshes on disk, owned by the page id that asked for them.

Flask-free; imports nothing from onshape_parts (a source's key arrives as a tuple). Each
part sits at <root>/<ref>/part.stl, `ref` a random 128-bit id; a ref from a request is
looked up in the index and never joined into a path without it. The index, like
ElementCache and JobPool, lives in process memory and assumes the one-worker Procfile.
So on start, as on every periodic sweep, ref-shaped directories under the root with no
index entry and older than the expiry are removed: a root that outlives a process (a
restart that reuses the folder) is cleaned too.
"""
import re
import secrets
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import limits

REF_RE = re.compile(r'^[A-Za-z0-9_-]{22}\Z')        # \Z: no trailing newline, unlike $
SWEEP_INTERVAL_S = 600


class PartLimit(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


@dataclass
class PartEntry:
    ref: str
    pid: str
    name: str
    source: dict
    stl_path: Path
    bytes: int
    triangles: int
    size_mm: dict
    footprints: dict
    last_used: float
    superseded: bool = False
    source_key: tuple = ()

    def to_json(self):
        return {'ref': self.ref, 'name': self.name, 'size_mm': self.size_mm,
                'triangles': self.triangles, 'source': self.source, 'footprints': self.footprints}


class PartStore:
    def __init__(self, root, *, clock=time.time, ttl_s=limits.PART_TTL_S, log=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._ttl_s = ttl_s
        self._log = log or (lambda *args: None)
        self._lock = threading.Lock()
        self._entries = {}          # ref -> PartEntry
        self.sweep()                # the start-up pass: stale directories of an earlier process

    # -- limits ---------------------------------------------------------

    def _live(self, pid):
        return [e for e in self._entries.values() if e.pid == pid and not e.superseded]

    def _total_bytes(self):
        return sum(e.bytes for e in self._entries.values())

    def check_room(self, pid, new_parts):
        """Raise PartLimit unless the page has room for `new_parts` more and the store is not full."""
        with self._lock:
            if len(self._live(pid)) + new_parts > limits.MAX_PARTS_PER_PAGE:
                raise PartLimit(limits.TOO_MANY_PARTS_MESSAGE)
            if self._total_bytes() >= limits.MAX_STORE_BYTES:
                raise PartLimit(limits.STORE_FULL_MESSAGE)

    # -- entries --------------------------------------------------------

    def add(self, pid, name, source, source_key, stl, mesh_info, replaces=None):
        """Store one exported mesh for `pid`; `mesh_info` has triangles, size_mm and footprints.

        `replaces` is the ref a refresh re-exported: it is left out of the page's byte sum and
        superseded in the same step, so a refresh never counts a part twice.
        """
        with self._lock:
            old = self._entries.get(replaces) if isinstance(replaces, str) else None
            old = old if old is not None and old.pid == pid else None
            page_bytes = sum(e.bytes for e in self._live(pid) if e is not old)
            if page_bytes + len(stl) > limits.MAX_PAGE_BYTES:
                raise PartLimit(limits.PAGE_BYTES_MESSAGE)
            if self._total_bytes() + len(stl) > limits.MAX_STORE_BYTES:
                raise PartLimit(limits.STORE_FULL_MESSAGE)
            ref = secrets.token_urlsafe(16)
            directory = self.root / ref
            directory.mkdir()
            stl_path = directory / 'part.stl'
            stl_path.write_bytes(stl)
            entry = PartEntry(ref=ref, pid=pid, name=name, source=source, stl_path=stl_path,
                              bytes=len(stl), triangles=mesh_info['triangles'],
                              size_mm=mesh_info['size_mm'], footprints=mesh_info['footprints'],
                              last_used=self._clock(), source_key=tuple(source_key))
            self._entries[ref] = entry
            if old is not None:
                old.superseded = True
            return entry

    def find_source(self, pid, source_key):
        """The page's live entry for this source (PartSource.key()), or None. A hit counts as
        a use: a part the student clicks again stays alive."""
        source_key = tuple(source_key)
        with self._lock:
            entry = next((e for e in self._live(pid) if e.source_key == source_key), None)
            if entry is not None:
                entry.last_used = self._clock()
            return entry

    def get(self, ref, pid):
        """The entry if `pid` owns `ref`, touching its last use; None otherwise."""
        if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
            return None
        with self._lock:
            entry = self._entries.get(ref)
            if entry is None or entry.pid != pid:
                return None
            entry.last_used = self._clock()
            return entry

    def supersede(self, refs, pid):
        """Mark the page's entries replaced by a refresh; their files stay until expiry."""
        with self._lock:
            for ref in refs:
                entry = self._entries.get(ref) if isinstance(ref, str) else None
                if entry is not None and entry.pid == pid:
                    entry.superseded = True

    # -- expiry ---------------------------------------------------------

    def sweep(self, now=None):
        """Drop entries unused for the TTL and orphan directories older than it; return the count."""
        now = self._clock() if now is None else now
        with self._lock:
            expired = [e for e in self._entries.values() if now - e.last_used >= self._ttl_s]
            for entry in expired:
                del self._entries[entry.ref]
            known = set(self._entries)
        removed = 0
        for entry in expired:
            shutil.rmtree(self.root / entry.ref, ignore_errors=True)
            removed += 1
        for path in self.root.iterdir():
            # Only directories the store could have made: a ref's name, not a symlink.
            if (path.name in known or not REF_RE.fullmatch(path.name) or path.is_symlink()
                    or not path.is_dir()):
                continue
            try:
                stale = now - path.stat().st_mtime > self._ttl_s
            except OSError:
                continue
            if stale:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed

    def start_sweeper(self, interval_s=SWEEP_INTERVAL_S):
        def loop():
            while True:
                time.sleep(interval_s)
                try:
                    self.sweep()
                except Exception:   # never let the sweeper die
                    self._log(traceback.format_exc())
        thread = threading.Thread(target=loop, name="print-parts-sweeper", daemon=True)
        thread.start()
        return thread
