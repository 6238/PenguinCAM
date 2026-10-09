"""Tests for testbed/cassette.py: the on-disk format of a recording."""
import json
import tempfile
import unittest
from pathlib import Path

from testbed.cassette import Cassette, Exchange


def _exchange(**fields):
    base = dict(method='GET', host='cad.onshape.com', path='/api/v13/documents',
                query={'q': ['tb-']}, request_body=None, status=200,
                headers={'Content-Type': 'application/json'}, body_text='{"items": []}',
                body_file=None)
    base.update(fields)
    return Exchange(**base)


class CassetteTest(unittest.TestCase):
    def test_cassette_roundtrip_with_binary_body(self):
        stl = bytes(range(256)) * 4
        exchanges = [
            _exchange(),
            _exchange(path='/api/v13/parts/d/a/m/b/e/c/partid/p/stl', status=307,
                      headers={'Location': 'https://cad.onshape.com/x'}, body_text=None),
            _exchange(path='/x', headers={'Content-Type': 'application/octet-stream'},
                      body_text=None, body_bytes=stl),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            Cassette('part-select', exchanges).save(directory)
            self.assertTrue((directory / 'part-select.json').is_file())
            self.assertEqual((directory / 'part-select' / '002.bin').read_bytes(), stl)
            raw = (directory / 'part-select.json').read_text()
            self.assertEqual(raw, json.dumps(json.loads(raw), indent=1, sort_keys=True) + '\n')

            loaded = Cassette.load(directory, 'part-select')
        self.assertEqual(loaded.scenario, 'part-select')
        self.assertFalse(loaded.synthetic)
        self.assertEqual(len(loaded.exchanges), 3)
        self.assertEqual(loaded.exchanges[0], exchanges[0])
        self.assertEqual(loaded.exchanges[2].body_file, 'part-select/002.bin')
        self.assertEqual(loaded.exchanges[2].body_bytes, stl)
        self.assertEqual(loaded.exchanges[2].body(), stl)
        self.assertEqual(loaded.exchanges[0].body(), b'{"items": []}')
        self.assertEqual(loaded.exchanges[1].body(), b'')

    def test_resave_leaves_no_orphan_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            two = [_exchange(body_text=None, body_bytes=b'a'), _exchange(body_text=None, body_bytes=b'b')]
            Cassette('s', two).save(directory)
            Cassette('s', two[:1]).save(directory)
            self.assertEqual(sorted(p.name for p in (directory / 's').iterdir()), ['000.bin'])

    def test_counted_calls_are_2xx_and_3xx(self):
        statuses = [200, 302, 399, 400, 402, 429, 503, 199]
        cassette = Cassette('s', [_exchange(status=s) for s in statuses], synthetic=True)
        self.assertEqual(cassette.counted_calls(), 3)


if __name__ == '__main__':
    unittest.main()
