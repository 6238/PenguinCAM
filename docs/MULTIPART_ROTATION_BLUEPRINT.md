# Blueprint: Free Rotation & Multi-Part Bed Placement

**Status:** Draft - Requesting Feedback
**Author:** Claude Code / Team 6238
**Date:** January 2026
**Target Version:** PenguinCAM 2.0

---

## Executive Summary

This blueprint proposes adding two major features to PenguinCAM:

1. **Free Rotation** - Allow parts to be rotated to any angle (not just 90° increments) using an intuitive visual interface inspired by PrusaSlicer
2. **Multi-Part Bed Placement** - Support multiple DXF files on the same bed with auto-nesting to minimize material waste

These features would bring PenguinCAM's workflow closer to modern 3D printer slicers, making it easier for students to optimize material usage and part orientation.

---

## Motivation

### Current Limitations

1. **Rotation restricted to 90° increments** - Some parts may fit better or machine more efficiently at other angles (e.g., 45°, 30°)
2. **Single part per job** - Students must manually calculate positions and run multiple jobs to cut several parts from one sheet
3. **Manual material optimization** - No tooling to help minimize scrap material

### Use Cases

| Scenario | Current Workflow | Proposed Workflow |
|----------|------------------|-------------------|
| Cutting 6 brackets from one sheet | Run 6 separate jobs, manually position each | Upload all 6 DXFs, auto-nest, one job |
| Part doesn't fit bed at 0° or 90° | Cut smaller or redesign | Rotate to optimal angle |
| Maximizing material usage | Mental math + trial and error | Visual bed view with auto-nesting |

---

## Feature Specifications

### 1. Free Rotation

#### 1.1 Visual Rotation Ring (PrusaSlicer-style)

A circular rotation control appears around the selected part, providing both precision and quick adjustment:

```
                    ┌─── 5° snap marks (outer)
                    │
              ╭─────┴─────╮
           ╭──┤  ╭─────╮  ├──╮
          ╱   │  │     │  │   ╲
         │    │  │ DXF │  │    │  ◄── Rotation ring
         │    │  │Part │  │    │
          ╲   │  │     │  │   ╱
           ╰──┤  ╰─────╯  ├──╯
              ╰─────┬─────╯
                    │
                    └─── 45° snap marks (inner)
```

**Interaction Model:**
- **Drag on outer zone** → Snaps to 5° increments
- **Drag on inner zone** → Snaps to 45° increments
- **Drag between zones** → Free rotation (no snapping)
- **Current angle indicator** → Yellow line from center

**Source:** This interaction pattern is directly inspired by [PrusaSlicer's Rotate Tool](https://help.prusa3d.com/article/move-rotate-scale-tools_1914), which has been refined over years of user feedback.

#### 1.2 Numeric Input Panel

For precise angle entry, a manipulation panel appears when a part is selected:

| Field | Type | Range | Notes |
|-------|------|-------|-------|
| Position X | Number | 0 to bed width | Bottom-left corner of part's bounding box |
| Position Y | Number | 0 to bed height | Bottom-left corner of part's bounding box |
| Rotation | Number | 0° to 359.9° | Clockwise from horizontal |
| Reset | Button | - | Returns part to 0° rotation |

#### 1.3 Technical Implementation

**Frontend Changes:**
- Rotation ring drawn on 2D canvas around selected part
- Mouse hit testing using polar coordinates to detect ring zone
- Angle calculation from mouse position relative to part center

**Backend Changes:**
- Change `--rotation` argument from `int` to `float` in post-processor
- Existing rotation math in `transform_coordinates()` already supports arbitrary angles (uses `sin`/`cos`)

**Rotation Math:**
```python
# Rotate point (x, y) around center (cx, cy) by angle_deg clockwise
angle_rad = -math.radians(angle_deg)  # Negative for clockwise
new_x = cx + (x - cx) * cos(angle_rad) - (y - cy) * sin(angle_rad)
new_y = cy + (x - cx) * sin(angle_rad) + (y - cy) * cos(angle_rad)
```

This is standard 2D rotation matrix multiplication. Reference: [Wikipedia - Rotation Matrix](https://en.wikipedia.org/wiki/Rotation_matrix)

---

### 2. Multi-Part Bed Placement

#### 2.1 Bed Configuration

Our CNC has specific constraints that should be visualized:

| Boundary | Dimensions | Color | Description |
|----------|------------|-------|-------------|
| Maximum bed | 25" × 25" | Dashed gray | Absolute machine limits |
| Preferred area | 25" × 21" | Solid yellow | Usable with tubing jig installed |
| Stock material | User-defined | Blue | Optional - actual material size |

```
┌─────────────────────────────────────┐
│                                     │ 25"
│  ┌─────────────────────────────┐   │
│  │     Preferred (25" × 21")   │   │ 21"
│  │                             │   │
│  │   ┌───┐  ┌───┐  ┌───┐      │   │
│  │   │ A │  │ B │  │ C │      │   │ ◄── Parts
│  │   └───┘  └───┘  └───┘      │   │
│  │                             │   │
│  └─────────────────────────────┘   │
│  ↑ Tubing jig area (avoid)         │ 4"
└─────────────────────────────────────┘
            25"
```

#### 2.2 Multi-File Upload

**UI Changes:**
- File input accepts multiple DXF files (`<input multiple>`)
- Parts list shows all uploaded files with:
  - Filename
  - Dimensions (rotated bounding box)
  - Remove button

**Part Selection:**
- Click part on canvas to select
- Selected part shows rotation ring
- Manipulation panel updates with part's values

**Part Positioning:**
- Drag-to-move on canvas
- Numeric position input for precision
- Collision detection warns if parts overlap

#### 2.3 Data Structure

```javascript
// Each part tracks its own state
class Part {
    id: string;              // Unique identifier
    filename: string;        // "bracket.dxf"
    dxfContent: string;      // Raw DXF text
    dxfGeometry: Object;     // Parsed entities

    // Transform state
    position: { x: number, y: number };  // Bottom-left corner on bed
    rotation: number;        // Degrees clockwise

    // Computed
    originalBounds: { width: number, height: number };

    getRotatedBounds(): { width: number, height: number } {
        // Axis-aligned bounding box after rotation
        const rad = Math.abs(this.rotation * Math.PI / 180);
        return {
            width: this.originalBounds.width * Math.cos(rad) +
                   this.originalBounds.height * Math.sin(rad),
            height: this.originalBounds.width * Math.sin(rad) +
                    this.originalBounds.height * Math.cos(rad)
        };
    }
}
```

---

### 3. Auto-Nesting Algorithm

#### 3.1 Overview

Auto-nesting automatically positions parts to minimize material waste while maintaining required spacing (tool diameter) between parts.

**Algorithm:** Bottom-Left Fill with Rotation Optimization

This is a well-established 2D bin packing heuristic that:
1. Sorts parts by area (largest first)
2. For each part, tries multiple rotations (0°, 90°, 180°, 270°)
3. Places each part at the lowest-leftmost valid position
4. Maintains minimum spacing between parts

**Source:** The Bottom-Left algorithm is a classic bin packing heuristic. See:
- [A Thousand Ways to Pack the Bin - Jukka Jylänki (2010)](http://pds25.egloos.com/pds/201504/21/98/RectangleBinPack.pdf)
- [Wikipedia - Bin Packing Problem](https://en.wikipedia.org/wiki/Bin_packing_problem)

#### 3.2 Algorithm Pseudocode

```
function autoNest(parts, bedWidth, bedHeight, toolDiameter):
    spacing = toolDiameter
    placed = []

    # Sort by area (largest first - generally gives better packing)
    sortedParts = sort(parts, by: width * height, descending)

    for part in sortedParts:
        bestPlacement = null
        bestY = infinity

        # Try each rotation
        for rotation in [0, 90, 180, 270]:
            bounds = getRotatedBounds(part, rotation)

            # Scan from bottom-left, find first valid position
            for y from 0 to (bedHeight - bounds.height) step 0.25":
                for x from 0 to (bedWidth - bounds.width) step 0.25":
                    if not collides(x, y, bounds, placed, spacing):
                        if y + bounds.height < bestY:
                            bestPlacement = {x, y, rotation, bounds}
                            bestY = y + bounds.height
                        break  # Found position at this Y, try next rotation

        if bestPlacement:
            placed.append(bestPlacement)
        else:
            error("Part doesn't fit on bed")

    return placed
```

#### 3.3 Collision Detection

Simple axis-aligned bounding box (AABB) check with spacing margin:

```
function collides(x, y, bounds, placed, spacing):
    for other in placed:
        if NOT (x + bounds.width + spacing <= other.x OR
                other.x + other.bounds.width + spacing <= x OR
                y + bounds.height + spacing <= other.y OR
                other.y + other.bounds.height + spacing <= y):
            return true  # Boxes overlap
    return false
```

**Note:** This uses bounding boxes, not exact geometry. A part rotated 45° will have a larger bounding box than its actual shape, resulting in slightly conservative (more spacing than strictly necessary) placements. This is acceptable for our use case and significantly simpler than true polygon intersection.

#### 3.4 Spacing Requirement

The minimum spacing between parts equals the tool diameter because:
- When cutting the perimeter, the tool center follows an offset path
- Tool compensation moves the path outward by `tool_radius`
- Two adjacent parts need `tool_radius + tool_radius = tool_diameter` gap

```
        Part A                    Part B
    ┌──────────┐              ┌──────────┐
    │          │              │          │
    │          │◄──spacing───►│          │
    │          │  (tool dia)  │          │
    └──────────┘              └──────────┘
         │                          │
         └──── Tool path ───────────┘
              (offset outward)
```

---

### 4. G-code Generation for Multiple Parts

#### 4.1 Operation Order

For efficiency and safety, operations are ordered:

1. **All holes** (across all parts) - sorted by proximity
2. **All pockets** (across all parts) - sorted by proximity
3. **All perimeters** (each part separately with its own tabs)

This minimizes rapid travel distance and ensures interior features are cut before perimeters (so parts don't shift mid-cut).

#### 4.2 Tab Handling

Each perimeter gets its own tab set:

```gcode
(===== PERIMETERS WITH TABS =====)

(Part 1: bracket.dxf)
G0 X1.5 Y2.0        ; Rapid to part 1 perimeter
G1 Z-0.25 F10       ; Plunge
G1 X5.0 Y2.0        ; Cut perimeter
G1 Z-0.15           ; Tab (rise)
G1 X5.5 Y2.0        ; Over tab
G1 Z-0.25           ; Resume depth
...

(Part 2: plate.dxf)
G0 X8.0 Y0.5        ; Rapid to part 2 perimeter
G1 Z-0.25 F10       ; Plunge
...
```

#### 4.3 Tool Path Optimization

Holes and pockets are sorted using nearest-neighbor algorithm to minimize rapid moves:

```python
def sort_by_proximity(operations):
    """Sort operations to minimize travel distance"""
    sorted_ops = []
    current_pos = (0, 0)  # Start at origin
    remaining = list(operations)

    while remaining:
        nearest = min(remaining,
                      key=lambda op: distance(current_pos, op.center))
        sorted_ops.append(nearest)
        current_pos = nearest.center
        remaining.remove(nearest)

    return sorted_ops
```

**Source:** Nearest-neighbor is a simple greedy heuristic for the Traveling Salesman Problem. While not optimal, it typically produces paths within 25% of optimal and is fast to compute. See: [Wikipedia - Nearest Neighbour Algorithm](https://en.wikipedia.org/wiki/Nearest_neighbour_algorithm)

---

## User Interface Mockup

```
┌─────────────────────────────────────────────────────────────────────┐
│  PenguinCAM                                              [Logout]   │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────────┐  ┌─────────────────────────────────┐  │
│  │  Material: [Plywood ▼]  │  │                                 │  │
│  │  Thickness: [0.25  ]    │  │     ┌───────────────────────┐   │  │
│  │  Tool Dia:  [0.157 ]    │  │     │                       │   │  │
│  │  Tabs:      [4     ]    │  │     │   ┌───┐    ╭───╮      │   │  │
│  │                         │  │     │   │ A │    │ B ○      │   │  │
│  │  ─────────────────────  │  │     │   └───┘    ╰───╯      │   │  │
│  │  Parts on Bed:          │  │     │          ◄─rotation   │   │  │
│  │  ┌────────────────────┐ │  │     │             ring      │   │  │
│  │  │ bracket.dxf   2"×3"│ │  │     │   ┌─────┐             │   │  │
│  │  │ [×]                │ │  │     │   │  C  │             │   │  │
│  │  ├────────────────────┤ │  │     │   └─────┘             │   │  │
│  │  │ plate.dxf     4"×2"│ │  │     │                       │   │  │
│  │  │ [×]         [sel]  │ │  │     └───────────────────────┘   │  │
│  │  ├────────────────────┤ │  │     ↑ 25" × 21" (preferred)     │  │
│  │  │ gusset.dxf   1"×1" │ │  │                                 │  │
│  │  │ [×]                │ │  │  Origin (0,0) ●                  │  │
│  │  └────────────────────┘ │  │                                 │  │
│  │                         │  └─────────────────────────────────┘  │
│  │  [+ Add Parts]          │                                       │
│  │  [Auto-Nest]            │  Selected: plate.dxf                  │
│  │                         │  Position: X [5.0] Y [2.0]            │
│  │  ─────────────────────  │  Rotation: [45.0]° [Reset]            │
│  │                         │                                       │
│  │  [Generate G-code]      │                                       │
│  │                         │                                       │
│  └─────────────────────────┘                                       │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## API Changes

### New Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/nest-parts` | POST | Auto-nest parts and return placements |
| `/api/validate-placement` | POST | Check for collisions and boundary violations |

### Modified Endpoints

**`/process` (POST)** - Now accepts multi-part configuration:

```json
// Request (multi-part mode)
{
    "parts_json": "[{\"filename\": \"bracket.dxf\", \"position\": {\"x\": 0, \"y\": 0}, \"rotation\": 45.0}, ...]",
    "material": "plywood",
    "thickness": "0.25",
    "tool_diameter": "0.157",
    "tabs": "4"
}
// + files[] multipart form data

// Response (same as current)
{
    "gcode": "...",
    "filename": "output_20260107_143022.nc",
    "cycle_time": "12:34"
}
```

**Backward Compatibility:** If `parts_json` is absent, existing single-file behavior is used.

---

## Testing Strategy

### Unit Tests

| Test | Description |
|------|-------------|
| `test_arbitrary_rotation` | Verify rotation math works for non-90° angles |
| `test_bounding_box_calculation` | Verify rotated bounding boxes are correct |
| `test_collision_detection` | Verify AABB collision with spacing |
| `test_auto_nest_simple` | 3 rectangles fit on bed |
| `test_auto_nest_tight` | Parts require rotation to fit |
| `test_auto_nest_overflow` | Proper error when parts don't fit |

### Integration Tests

| Test | Description |
|------|-------------|
| `test_multi_part_gcode` | Generate G-code for 3 parts, verify structure |
| `test_hole_ordering` | Verify holes sorted by proximity across parts |
| `test_tab_per_part` | Each perimeter has independent tabs |

### Manual Testing

- [ ] Rotation ring snapping feels natural
- [ ] Drag-to-position is responsive
- [ ] Auto-nest produces reasonable layouts
- [ ] Generated G-code machines correctly (dry run)

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Complex mouse interactions on canvas | High - frustrating UX | Extensive manual testing, iterate on feel |
| Bounding box nesting wastes material for irregular parts | Medium - suboptimal | Accept for V1, consider polygon nesting for V2 |
| Multi-part G-code errors | High - machine damage | Comprehensive test suite, dry-run validation |
| Performance with many parts | Low - unlikely | Canvas rendering is fast; limit to ~20 parts |

---

## Future Enhancements (Out of Scope)

These are explicitly **not** included in this proposal but could be future work:

1. **True polygon nesting** - Use actual part geometry instead of bounding boxes for tighter packing
2. **Material cost estimation** - Calculate scrap percentage and material cost
3. **Part duplication** - "Add 5 copies of this part" button
4. **Save/load layouts** - Persist bed arrangements for reuse
5. **Different parameters per part** - Individual tab counts, depths, etc.

---

## Questions for Discussion

1. **Rotation increments:** Should auto-nest try more rotations (0°, 15°, 30°, 45°, ...) for better packing, or stick with 90° increments for simplicity?

2. **Stock size input:** Should we require users to enter their stock dimensions, or make it optional with just bed limits enforced?

3. **Maximum parts:** Should we limit the number of parts per job? Suggested: 20 parts maximum.

4. **Mobile/touch support:** Is touch interaction (tablets) a requirement, or desktop-only acceptable?

5. **Undo/redo:** Should part movements and rotations be undoable?

---

## References

1. **PrusaSlicer Rotate Tool** - https://help.prusa3d.com/article/move-rotate-scale-tools_1914
2. **PrusaSlicer Auto-Arrange** - https://help.prusa3d.com/article/auto-arrange-tool_1770
3. **PrusaSlicer Object Manipulation** - https://help.prusa3d.com/article/object-manipulation-panel_1757
4. **Bin Packing Algorithms** - http://pds25.egloos.com/pds/201504/21/98/RectangleBinPack.pdf
5. **2D Rotation Matrix** - https://en.wikipedia.org/wiki/Rotation_matrix
6. **Nearest Neighbor TSP** - https://en.wikipedia.org/wiki/Nearest_neighbour_algorithm

---

## Appendix A: Affected Files

| File | Type of Change |
|------|----------------|
| `static/app.js` | Major - Multi-part state, rotation ring, bed view |
| `templates/index.html` | Moderate - Parts list, manipulation panel, multi-upload |
| `frc_cam_gui_app.py` | Moderate - New routes, multi-part processing |
| `frc_cam_postprocessor.py` | Moderate - Float rotation, multi-part G-code |
| `static/style.css` | Minor - New component styles |

---

## Appendix B: Estimated Effort

| Phase | Components | Complexity |
|-------|------------|------------|
| 1. Data structures | Part class, state refactor | Low |
| 2. Free rotation UI | Ring drawing, mouse handling, snapping | High |
| 3. Multi-file upload | HTML changes, file handling | Low |
| 4. Bed view rendering | Grid, boundaries, multi-part display | Medium |
| 5. Backend processing | Routes, G-code generation | Medium |
| 6. Auto-nesting | Algorithm, API | Medium |
| 7. Testing & polish | Unit tests, UX refinement | Medium |

---

*This blueprint is a living document. Please add comments or questions directly to this file or discuss in team meetings.*
