"""The part store: page id ownership, limits, superseding, expiry and the orphan sweep."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]        # the repository root
sys.path.insert(0, str(REPO))

from print import limits  # noqa: E402
from print.part_store import REF_RE, PartLimit, PartStore  # noqa: E402

PID, OTHER = "abcdef012345", "0123456789ab"
INFO = {"triangles": 12, "size_mm": {"x": 10.0, "y": 20.0, "z": 30.0}, "footprints": {"+z": {}}}


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


class PartStoreTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = Clock()
        self.store = PartStore(self.root, clock=self.clock)

    def add(self, pid=PID, stl=b"x" * 100, key=None, name="Box"):
        return self.store.add(pid, name, {"partId": "JHD"}, key or ("d", "w", "e", name), stl, INFO)

    def test_add_get_owner_only(self):
        entry = self.add()
        self.assertRegex(entry.ref, REF_RE)
        self.assertEqual(entry.stl_path, self.root / entry.ref / "part.stl")
        self.assertEqual(entry.stl_path.read_bytes(), b"x" * 100)
        self.assertEqual(self.store.get(entry.ref, PID), entry)
        self.assertIsNone(self.store.get(entry.ref, OTHER))
        self.assertEqual(entry.to_json(), {"ref": entry.ref, "name": "Box", "size_mm": INFO["size_mm"],
                                           "triangles": 12, "source": {"partId": "JHD"},
                                           "footprints": INFO["footprints"]})
        self.assertIs(self.store.find_source(PID, ("d", "w", "e", "Box")), entry)
        self.assertIsNone(self.store.find_source(OTHER, ("d", "w", "e", "Box")))

    def test_bad_ref_never_touches_disk(self):
        (self.root / ("a" * 22)).mkdir()                 # on disk but not in the index
        # Any path built during get() would fail the test: a ref is only ever an index key.
        with mock.patch.object(Path, "__truediv__", side_effect=AssertionError("path built")), \
                mock.patch.object(Path, "exists", side_effect=AssertionError("disk read")):
            for ref in ("../x", "a" * 22, "", None, "a" * 21, "a" * 22 + "/", "a" * 22 + "\n"):
                with self.subTest(ref=ref):
                    self.assertIsNone(self.store.get(ref, PID))
        self.assertIsNone(REF_RE.fullmatch("a" * 22 + "\n"))

    def test_twenty_parts_per_page(self):
        for n in range(limits.MAX_PARTS_PER_PAGE):
            self.store.check_room(PID, 1)
            self.add(key=("k", n), name=f"P{n}")
        with self.assertRaises(PartLimit) as caught:
            self.store.check_room(PID, 1)
        self.assertEqual(caught.exception.message, "Up to 20 different parts per print.")
        self.store.check_room(OTHER, limits.MAX_PARTS_PER_PAGE)     # per page id

    def test_page_bytes_and_store_cap(self):
        half = b"x" * (limits.MAX_PAGE_BYTES // 2)
        self.add(stl=half, key=("a",))
        self.add(stl=half, key=("b",))
        with self.assertRaises(PartLimit) as caught:
            self.add(stl=b"x", key=("c",))
        self.assertEqual(caught.exception.message, "These parts are too large to slice together.")
        store = PartStore(self.root / "small", clock=self.clock)
        store_cap = limits.MAX_STORE_BYTES
        try:
            limits.MAX_STORE_BYTES = 250
            store.add(PID, "A", {}, ("a",), b"x" * 200, INFO)
            with self.assertRaises(PartLimit) as caught:
                store.add(OTHER, "B", {}, ("b",), b"x" * 100, INFO)
            self.assertEqual(caught.exception.message, "The print service is busy; try again in a few minutes.")
            store.add(OTHER, "B", {}, ("b",), b"x" * 50, INFO)          # exactly at the cap
            with self.assertRaises(PartLimit) as caught:
                store.check_room(OTHER, 1)
            self.assertEqual(caught.exception.message, limits.STORE_FULL_MESSAGE)
        finally:
            limits.MAX_STORE_BYTES = store_cap

    def test_superseded_not_counted_but_file_kept(self):
        entries = [self.add(key=("k", n), name=f"P{n}") for n in range(limits.MAX_PARTS_PER_PAGE)]
        self.store.supersede([e.ref for e in entries], OTHER)          # not the owner: no effect
        with self.assertRaises(PartLimit):
            self.store.check_room(PID, 1)
        self.store.supersede([entries[0].ref, "../x"], PID)
        self.store.check_room(PID, 1)
        self.assertTrue(entries[0].superseded)
        self.assertTrue(entries[0].stl_path.exists())
        self.assertIs(self.store.get(entries[0].ref, PID), entries[0])   # a queued job still reads it
        self.assertIsNone(self.store.find_source(PID, ("k", 0)))

    def test_expiry_one_hour_after_last_use(self):
        entry = self.add()
        self.clock.now += 59 * 60
        self.assertIsNotNone(self.store.get(entry.ref, PID))
        self.clock.now += 59 * 60                       # 118 min after adding, 59 after the get
        self.assertEqual(self.store.sweep(), 0)
        self.assertTrue(entry.stl_path.exists())
        self.clock.now += 2 * 60
        self.assertEqual(self.store.sweep(), 1)
        self.assertFalse((self.root / entry.ref).exists())
        self.assertIsNone(self.store.get(entry.ref, PID))

    def test_orphan_directories_older_than_an_hour_deleted(self):
        live = self.add()
        old, new = self.root / ("o" * 22), self.root / ("n" * 22)
        for directory in (old, new):
            directory.mkdir()
            (directory / "part.stl").write_bytes(b"x")
        stale = self.clock.now - limits.PART_TTL_S - 10
        os.utime(old, (stale, stale))
        unrelated = self.root / "not-a-ref"                # a name the store never makes: left alone
        unrelated.mkdir()
        os.utime(unrelated, (stale, stale))
        self.assertEqual(self.store.sweep(), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(live.stl_path.exists())


class StartUpSweepTest(unittest.TestCase):
    def test_stale_ref_directories_removed_on_start(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        clock = Clock()
        old, new, other = root / ("o" * 22), root / ("n" * 22), root / "print_job_scratch"
        for directory in (old, new, other):
            directory.mkdir()
        stale = clock.now - limits.PART_TTL_S - 10
        for directory in (old, other):
            os.utime(directory, (stale, stale))
        PartStore(root, clock=clock)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(other.exists())             # not a name the store makes


if __name__ == "__main__":
    unittest.main()
