"""One plate job: the copies, the parts they use, the profiles, and running the slice.
Flask-free.

print/routes.py builds a PlateJob once the placement has passed every check and hands it to
the job pool as the job's payload; the pool's worker calls run_plate_job in the job's own
scratch directory.
"""
from dataclasses import dataclass
from pathlib import Path

from .plate_3mf import check_plate_names, plate_object_names, write_plate_3mf
from .slicer import SliceProfiles, slice_plate

MODEL_FILE = "plate.3mf"
SLICE_DIR = "slice"


@dataclass
class JobPart:
    name: str
    stl_path: Path
    footprints: dict
    triangles: int


@dataclass
class PlateJob:
    copies: list[dict]
    parts: dict[str, JobPart]
    profiles: SliceProfiles
    overrides: dict[str, str]
    filament: str
    process: str
    delivered_name: str
    pid: str | None
    team: int | None

    def triangles(self):
        return sum(self.parts[copy['ref']].triangles for copy in self.copies)


def run_plate_job(job, scratch):
    """Write the plate's 3MF, slice it with the job's profiles and check that Orca sliced
    every copy. Returns the SliceResult; raises SliceError.

    The process file is the profile's own: overrides are not applied yet.
    """
    scratch = Path(scratch)
    model = scratch / MODEL_FILE
    parts = {ref: {'name': part.name, 'stl_path': part.stl_path, 'footprints': part.footprints}
             for ref, part in job.parts.items()}
    names = write_plate_3mf(model, job.copies, parts)
    result = slice_plate(model, job.profiles, scratch / SLICE_DIR)
    check_plate_names(names, plate_object_names(result.output_path))
    return result
