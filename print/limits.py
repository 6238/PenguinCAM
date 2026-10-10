"""Limits of the print path from Onshape, and the sentences that refuse them. Flask-free, no imports.

One source for every cap the routes, the part store and the plate writer enforce, and
for the sentence the student sees when one is reached (spec section 5); each sentence
that names a number is formatted from the cap, so the two cannot differ.
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
MAX_COPIES = 25                                   # 30 peaked 1.5% under the 900 MB rule; 25 leaves headroom
MAX_QUANTITY = MAX_COPIES
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

# The sentences, one per limit. {name} is filled in by the caller.
TOO_DETAILED_MESSAGE = ("{name} is too detailed to print here "
                        f"(over {MAX_PART_TRIANGLES:,} triangles).")
TOO_MANY_PARTS_MESSAGE = f"Up to {MAX_PARTS_PER_PAGE} different parts per print."
PAGE_BYTES_MESSAGE = "These parts are too large to slice together."
TOO_MANY_COPIES_MESSAGE = f"Up to {MAX_COPIES} copies per plate; print the rest in a second job."
JOB_TRIANGLES_MESSAGE = "These copies are too detailed to slice together; lower the quantities."
STORE_FULL_MESSAGE = "The print service is busy; try again in a few minutes."
ONE_PLATE_MESSAGE = ("Not every copy fits on one plate; lower the quantities and print the "
                     "rest in a second job.")
TOO_SMALL_MESSAGE = "{name} is too small to print (under 0.1 mm)."
MIN_PART_EXTENT_MM = 0.1                          # TOO_SMALL_MESSAGE's 0.1 mm
EXPORT_FAILED_MESSAGE = "Onshape could not export {name}. Try again, or Refresh from Onshape."


def limits_for_browser():
    """The caps and sentences the Layout step checks before it asks for a slice."""
    return {
        "max_copies": MAX_COPIES,
        "max_quantity": MAX_QUANTITY,
        "max_job_triangles": MAX_JOB_TRIANGLES,
        "too_many_copies_message": TOO_MANY_COPIES_MESSAGE,
        "job_triangles_message": JOB_TRIANGLES_MESSAGE,
        "one_plate_message": ONE_PLATE_MESSAGE,
    }
