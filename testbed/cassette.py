"""The cassette: one scenario's recorded Onshape exchanges, as committed to the repository.

A cassette is `<dir>/<scenario>.json`. Binary response bodies (STL and other exported files)
sit next to it as `<dir>/<scenario>/NNN.bin`, NNN being the exchange's index.
"""
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

# The only response headers a cassette keeps.
KEPT_HEADERS = ('X-Rate-Limit-Remaining', 'Location', 'Content-Type')


@dataclass
class Exchange:
    method: str
    host: str
    path: str                                # the full URL path, e.g. /api/v13/parts/d/.../stl
    query: dict                              # dict[str, list[str]]
    request_body: Optional[str]
    status: int
    headers: dict                            # only KEPT_HEADERS
    body_text: Optional[str]
    body_file: Optional[str]                 # relative to the cassette directory
    # A binary body in memory: set while recording and filled from body_file on load.
    # Never written to the JSON.
    body_bytes: Optional[bytes] = field(default=None, repr=False, compare=False)

    def body(self) -> bytes:
        if self.body_bytes is not None:
            return self.body_bytes
        if self.body_text is not None:
            return self.body_text.encode('utf-8')
        return b''

    def to_json(self) -> dict:
        data = asdict(self)
        del data['body_bytes']
        return data

    @classmethod
    def from_json(cls, data: dict) -> 'Exchange':
        names = {f.name for f in fields(cls)} - {'body_bytes'}
        return cls(**{name: data.get(name) for name in names})


class Cassette:
    def __init__(self, scenario: str, exchanges: list, synthetic: bool = False):
        self.scenario = scenario
        self.exchanges = exchanges
        self.synthetic = synthetic           # hand-written for a unit test, not recorded

    def counted_calls(self) -> int:
        """The exchanges Onshape counts against the allowance: statuses 200 to 399."""
        return sum(1 for ex in self.exchanges if 200 <= ex.status < 400)

    def save(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        records = []
        for index, ex in enumerate(self.exchanges):
            record = ex.to_json()
            if ex.body_bytes is not None:
                relative = f'{self.scenario}/{index:03d}.bin'
                (directory / self.scenario).mkdir(exist_ok=True)
                (directory / relative).write_bytes(ex.body_bytes)
                record['body_file'] = relative
                record['body_text'] = None
            records.append(record)
        path = directory / f'{self.scenario}.json'
        document = {'scenario': self.scenario, 'synthetic': self.synthetic,
                    'exchanges': records}
        path.write_text(json.dumps(document, indent=1, sort_keys=True))
        return path

    @classmethod
    def load(cls, directory: Path, scenario: str) -> 'Cassette':
        directory = Path(directory)
        document = json.loads((directory / f'{scenario}.json').read_text())
        exchanges = []
        for record in document['exchanges']:
            ex = Exchange.from_json(record)
            if ex.body_file:
                ex.body_bytes = (directory / ex.body_file).read_bytes()
            exchanges.append(ex)
        return cls(document['scenario'], exchanges, document.get('synthetic', False))
