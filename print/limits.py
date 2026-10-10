"""Numeric limits of the print path from Onshape. Flask-free, no imports.

One source for every cap the routes, the part store and the plate writer enforce.
The caps were set from slices measured on an aarch64 Linux machine; the plates
measured at these limits are recorded in print/jobs.py's sizing note.
"""

# One part, as exported from Onshape.
MAX_PART_TRIANGLES = 150_000
MAX_PART_BYTES = 84 + 50 * MAX_PART_TRIANGLES     # a binary STL of exactly that many triangles

# One page id (one load of the print page).
MAX_PARTS_PER_PAGE = 20
MAX_PAGE_BYTES = 50_000_000

# One plate, one job.
MAX_COPIES = 30
MAX_QUANTITY = 30
MAX_JOB_TRIANGLES = 300_000                       # summed over every copy on the plate
PLATE_SLICE_TIMEOUT_S = 240                       # the sample part keeps slicer.DEFAULT_TIMEOUT_S

# The part store on disk, across every page id.
MAX_STORE_BYTES = 500_000_000
PART_TTL_S = 3600

# Every STL export asks for this tessellation explicitly: Onshape documents the units
# (metres; radians, under pi/2) but not its defaults, so the print path never relies on them.
STL_CHORD_TOLERANCE_M = 0.00005                   # 0.05 mm
STL_ANGLE_TOLERANCE_RAD = 0.1309                  # 7.5 degrees: 48 segments on every hole

# The stop rule for print/scripts/measure_plate.py: a plate at the limits must stay
# inside both, or MAX_JOB_TRIANGLES comes down.
MEASURE_MAX_PEAK_KB = 921_600                     # 900 MB, as Linux reports ru_maxrss
MEASURE_MAX_SECONDS = 180
