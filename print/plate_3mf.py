"""The plate as a 3MF Orca slices without moving anything, and the checks on what it sliced.
Flask-free.

One <object> per copy, never one object shared by several build items: Orca blanks the
names of copies that share an object, and the names are what the printer's skip-object
list shows and what the name guard below reads back. Each object holds its part's mesh in
part coordinates; its build item carries copy_matrix (print/plate.py) in 3MF's row-vector
order. Orca drops every part to the bed and, with --arrange 0, keeps x, y and rotation.
"""
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr

from .mesh import read_binary_stl
from .plate import copy_labels, copy_matrix, sanitize_part_name
from .slicer import ARCHIVE_SUFFIX, SliceError

MODEL_ENTRY = "3D/3dmodel.model"
PLATE_JSON_ENTRY = "Metadata/plate_1.json"
LOCAL_TIME_RE = re.compile(r'^\d{8}-\d{4}$')
LEFT_OFF_MESSAGE = "A part was left off the plate"
UNREADABLE_MESSAGE = "The slicer output could not be read."

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>
"""
RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
MODEL_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
              '<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n<resources>\n')


def _mesh_xml(stl_path, name):
    """The part's <mesh> element as bytes: de-duplicated vertices (%.4f) and faces."""
    mesh = read_binary_stl(Path(stl_path).read_bytes(), name)
    vertices = "".join('<vertex x="%.4f" y="%.4f" z="%.4f"/>' % tuple(v) for v in mesh.vertices.tolist())
    triangles = "".join('<triangle v1="%d" v2="%d" v3="%d"/>' % tuple(f) for f in mesh.faces.tolist())
    return f"<mesh><vertices>{vertices}</vertices><triangles>{triangles}</triangles></mesh>".encode("ascii")


def _transform(copy, footprint):
    """copy_matrix as 3MF's twelve numbers: M's columns, then the translation."""
    m = copy_matrix(copy['orientation'], copy['angle'], footprint['offset'], copy['x'], copy['y'])
    order = [m[r][c] for c in range(3) for r in range(3)] + [m[0][3], m[1][3], m[2][3]]
    return " ".join("%.9g" % (value + 0.0) for value in order)


def write_plate_3mf(path, copies, parts):
    """Write the plate's 3MF to `path` and return its object names, one per copy.

    `copies` are {'ref', 'orientation', 'angle', 'x', 'y'}; `parts[ref]` carries `name`,
    `stl_path` and `footprints`. Each part's STL is read once, its mesh written once per copy.
    """
    names = copy_labels([parts[copy['ref']]['name'] for copy in copies])
    meshes = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        with zf.open(MODEL_ENTRY, "w") as model:
            model.write(MODEL_HEAD.encode("ascii"))
            for object_id, (copy, name) in enumerate(zip(copies, names), start=1):
                part = parts[copy['ref']]
                if copy['ref'] not in meshes:
                    meshes[copy['ref']] = _mesh_xml(part['stl_path'], part['name'])
                model.write(f'<object id="{object_id}" name={quoteattr(name)} type="model">'.encode("ascii"))
                model.write(meshes[copy['ref']])
                model.write(b"</object>\n")
            model.write(b"</resources>\n<build>\n")
            for object_id, copy in enumerate(copies, start=1):
                footprint = parts[copy['ref']]['footprints'][copy['orientation']]
                model.write(f'<item objectid="{object_id}" transform={quoteattr(_transform(copy, footprint))}/>\n'
                            .encode("ascii"))
            model.write(b"</build>\n</model>\n")
    return names


def delivered_file_name(part_names, local_time, now=None):
    """`<first part>[_plus<N>]-<YYYYMMDD-HHMM>.gcode.3mf`; `part_names` are the distinct
    parts in copy order. The browser's local time when it matches LOCAL_TIME_RE, else UTC."""
    name = sanitize_part_name(part_names[0])
    if len(part_names) > 1:
        name += f"_plus{len(part_names) - 1}"
    if not (isinstance(local_time, str) and LOCAL_TIME_RE.fullmatch(local_time)):
        local_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"{name}-{local_time}{ARCHIVE_SUFFIX}"


def plate_object_names(archive):
    """The object names Orca sliced onto plate 1, from the archive's Metadata/plate_1.json."""
    try:
        with zipfile.ZipFile(archive) as zf:
            plate = json.loads(zf.read(PLATE_JSON_ENTRY).decode("utf-8"))
        return [str(obj.get("name") or "") for obj in plate.get("bbox_objects", [])]
    except (OSError, KeyError, ValueError, AttributeError, zipfile.BadZipFile) as exc:
        raise SliceError(UNREADABLE_MESSAGE, f"{PLATE_JSON_ENTRY}: {exc!r}") from None


def check_plate_names(expected, found):
    """Raise SliceError(LEFT_OFF_MESSAGE) unless Orca sliced exactly the expected objects.

    Orca leaves a part that is entirely off the plate out of the slice without an error;
    this is what notices."""
    if sorted(found) == sorted(expected) and all(name.strip() for name in found):
        return
    missing = sorted(set(expected) - set(found))
    extra = sorted(set(found) - set(expected))
    raise SliceError(LEFT_OFF_MESSAGE, f"expected {len(expected)} objects, found {len(found)}; "
                                       f"missing {missing}; unexpected {extra}")
