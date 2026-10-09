"""Exported mesh checks: the size of an STL Onshape exported, in millimetres (spec 5.7)."""
from print.slicer import stl_bounds

_MM_PER_UNIT = {'millimeter': 1.0, 'inch': 25.4}


def mesh_size_mm(stl_bytes: bytes, units: str) -> tuple:
    """The mesh's bounding-box extents in millimetres, smallest first.

    `units` is the unit the file was exported in: 'millimeter', or 'inch', Onshape's
    default whatever the document's units. Sorted, so a part's orientation does not matter.
    """
    if units not in _MM_PER_UNIT:
        raise ValueError(f"units must be 'millimeter' or 'inch', not {units!r}")
    lo, hi, _count = stl_bounds(stl_bytes)
    scale = _MM_PER_UNIT[units]
    return tuple(sorted((hi[i] - lo[i]) * scale for i in range(3)))
