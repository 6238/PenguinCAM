# FRC CAM Post-Processor GUI - Visual Guide

## Interface Overview

The GUI is organized into a clear 4-step workflow:

```
┌─────────────────────────────────────────────────────────────────────┐
│  FRC CAM Post-Processor                                             │
│  Generate G-code from OnShape DXF exports for your FRC robotics CNC│
└─────────────────────────────────────────────────────────────────────┘

┌────────────────────────┬────────────────────────────────────────────┐
│                        │                                            │
│  ┌─ STEP 1 ─────────┐ │  ┌─ STEP 4 ───────────────────────────┐   │
│  │ Upload DXF File   │ │  │ Preview & Download                 │   │
│  │                   │ │  │                                     │   │
│  │ [Drag & Drop     ]│ │  │  ┌──────────────────────────────┐  │   │
│  │ [   Area        ]│ │  │  │                              │  │   │
│  │ or click to      │ │  │  │     3D G-code Preview        │  │   │
│  │  browse          │ │  │  │     (Interactive)            │  │   │
│  └──────────────────┘ │  │  │                              │  │   │
│                        │  │  │  • Mouse to rotate           │  │   │
│  ┌─ STEP 2 ─────────┐ │  │  │  • Scroll to zoom            │  │   │
│  │ Set Parameters    │ │  │  └──────────────────────────────┘  │   │
│  │                   │ │  │                                     │   │
│  │ Thickness: 0.25   │ │  │  [Reset View] button               │   │
│  │ Tool Dia:  0.157  │ │  │                                     │   │
│  │ Sacrifice: 0.02   │ │  │  [Download G-code File]            │   │
│  │ Tabs:      4      │ │  └─────────────────────────────────────┘   │
│  │ □ Drill Screws    │ │                                            │
│  └──────────────────┘ │                                            │
│                        │                                            │
│  ┌─ STEP 3 ─────────┐ │                                            │
│  │ Generate G-code   │ │                                            │
│  │                   │ │                                            │
│  │ [🚀 Generate    ] │ │                                            │
│  │                   │ │                                            │
│  │ Results:          │ │                                            │
│  │ ✓ 8 Screw Holes   │ │                                            │
│  │ ✓ 304 G-code Lines│ │                                            │
│  └──────────────────┘ │                                            │
│                        │                                            │
└────────────────────────┴────────────────────────────────────────────┘
```

## Color Scheme

The GUI uses a professional dark theme optimized for long sessions:

- **Background:** Dark blue-gray (#0A0E14)
- **Panels:** Slightly lighter (#151B24)
- **Primary Accent:** Bright orange (#FF4500) - Robotics energy!
- **Text:** Light gray for readability
- **Success:** Green for completed actions
- **Error:** Red for warnings

## Step-by-Step Screenshots

### Step 1: Upload DXF File

```
┌──────────────────────────────────────┐
│  1  Upload DXF File                  │
├──────────────────────────────────────┤
│                                      │
│      📄                              │
│                                      │
│  Drag & drop your DXF file here     │
│  or click to browse                 │
│                                      │
│  Supported: .dxf files only         │
│                                      │
└──────────────────────────────────────┘

        ↓ After uploading ↓

┌──────────────────────────────────────┐
│  1  Upload DXF File                  │
├──────────────────────────────────────┤
│ File loaded: robot_plate.dxf        │
│ Size: 24.5 KB                       │
└──────────────────────────────────────┘
```

Visual feedback:
- ✓ Border changes to orange on hover
- ✓ File info appears after upload
- ✓ Filename and size displayed

### Step 2: Set Parameters

```
┌──────────────────────────────────────┐
│  2  Set Parameters                   │
├──────────────────────────────────────┤
│                                      │
│  Material Thickness (inches)        │
│  1/4" = 0.25                        │
│  ┌────────┐                         │
│  │ 0.25   │ ← Numeric input         │
│  └────────┘                         │
│                                      │
│  Tool Diameter (inches)             │
│  4mm = 0.157"                       │
│  ┌────────┐                         │
│  │ 0.157  │                         │
│  └────────┘                         │
│                                      │
│  Sacrifice Board Depth (inches)     │
│  Overcut depth                      │
│  ┌────────┐                         │
│  │ 0.02   │                         │
│  └────────┘                         │
│                                      │
│  Number of Tabs                     │
│  Holding tabs on perimeter          │
│  ┌────────┐                         │
│  │ 4      │                         │
│  └────────┘                         │
│                                      │
│  ☐ Center drill screw holes (faster)│
│                                      │
└──────────────────────────────────────┘
```

Features:
- ✓ Monospace font for numbers (technical feel)
- ✓ Helpful hints next to each parameter
- ✓ Input validation (min/max values)
- ✓ Checkbox for drill option

### Step 3: Generate G-code

```
┌──────────────────────────────────────┐
│  3  Generate G-code                  │
├──────────────────────────────────────┤
│                                      │
│  ┌──────────────────────────────┐   │
│  │  🚀 Generate G-code          │   │
│  └──────────────────────────────┘   │
│                                      │
└──────────────────────────────────────┘

        ↓ While processing ↓

┌──────────────────────────────────────┐
│  3  Generate G-code                  │
├──────────────────────────────────────┤
│         ⏳                           │
│    Processing your file...          │
└──────────────────────────────────────┘

        ↓ After success ↓

┌──────────────────────────────────────┐
│  3  Generate G-code                  │
├──────────────────────────────────────┤
│                                      │
│  ✓ G-code Generated Successfully    │
│                                      │
│  ┌────────┬────────┐                │
│  │Screw   │G-code  │                │
│  │Holes   │Lines   │                │
│  │   8    │  304   │                │
│  └────────┴────────┘                │
│                                      │
│  ▸ Show console output               │
│                                      │
└──────────────────────────────────────┘
```

Feedback states:
- ✓ Loading spinner during processing
- ✓ Success message with green accent
- ✓ Statistics in grid layout
- ✓ Expandable console output

### Step 4: Preview & Download

```
┌────────────────────────────────────────────────┐
│  4  Preview & Download                         │
├────────────────────────────────────────────────┤
│                                                │
│  ┌──────────────────────────────────────────┐ │
│  │                                          │ │
│  │          [3D Toolpath View]             │ │
│  │                                          │ │
│  │    Orange line shows tool movement      │ │
│  │    Grid shows work surface              │ │
│  │    Axes: X (red), Y (green), Z (blue)   │ │
│  │                                          │ │
│  │    • Click + drag to rotate             │ │
│  │    • Scroll to zoom                     │ │
│  │                                          │ │
│  └──────────────────────────────────────────┘ │
│                                                │
│  Use mouse to rotate • Scroll to zoom         │
│  [Reset View]                                 │
│                                                │
│  ┌──────────────────────────────────────────┐ │
│  │     Download G-code File                 │ │
│  └──────────────────────────────────────────┘ │
│                                                │
└────────────────────────────────────────────────┘
```

3D Visualization features:
- ✓ WebGL-based rendering (smooth)
- ✓ Orange toolpath (high visibility)
- ✓ Grid helper for scale reference
- ✓ Axis indicators (XYZ)
- ✓ Mouse controls (orbit, zoom)
- ✓ Auto-framing of part

## Responsive Design

The interface adapts to different screen sizes:

### Desktop (>1024px)
- Side-by-side layout
- Parameters on left, preview on right
- Large 3D visualization

### Tablet/Small Screens (<1024px)
- Stacked layout
- Steps flow vertically
- Smaller but still usable

## Interactive Elements

### Hover Effects

**Buttons:**
```
Normal:     [  Generate G-code  ]
Hover:      [  Generate G-code  ] ← Slightly raised, brighter
```

**Drop Zone:**
```
Normal:     ┌─────────────┐
            │ Drop here   │
            └─────────────┘

Hover:      ┌═════════════┐ ← Orange border
            ║ Drop here   ║    Scale up 2%
            └═════════════┘
```

### Drag & Drop

```
Dragging file over page:
┌═════════════════════════┐
║ Drop your file here!    ║ ← Highlighted
└═════════════════════════┘

File dropped:
✓ Filename appears
✓ File size shown
✓ Generate button enabled
```

### Loading States

**Spinner animation:**
```
    ⏳ 
Processing...

[Rotating circle with orange accent]
```

## Error Handling

### User-Friendly Errors

```
┌────────────────────────────────────────┐
│ ❌ Error                               │
├────────────────────────────────────────┤
│ Invalid file type:                    │
│ Please upload a DXF file.             │
│                                        │
│ Accepted formats: .dxf                │
└────────────────────────────────────────┘

OR

┌────────────────────────────────────────┐
│ ❌ Generation Failed                   │
├────────────────────────────────────────┤
│ Post-processor failed:                │
│ No closed paths found in DXF          │
│                                        │
│ Try simplifying your design in CAD.   │
└────────────────────────────────────────┘
```

Errors are:
- ✓ Clear and actionable
- ✓ Red color coding
- ✓ Suggest solutions
- ✓ Dismissible

## Accessibility Features

✓ **Keyboard Navigation**
  - Tab through all inputs
  - Enter to submit
  - Esc to cancel

✓ **High Contrast**
  - Dark mode reduces eye strain
  - Text has sufficient contrast ratio
  - Orange accent highly visible

✓ **Clear Labels**
  - Every input labeled
  - Hints provided
  - Units specified

✓ **Focus Indicators**
  - Orange outline on focused inputs
  - Clear active states
  - Visible keyboard focus

## Animations

Smooth transitions throughout:

**Panel Slide-In:** Results appear with smooth slide
**Spinner Rotation:** Continuous smooth rotation
**Button Hover:** Subtle lift on hover
**Drop Zone Scale:** Slight scale on dragover

All animations use CSS for performance!

## Technical Stack

**Frontend:**
- HTML5
- CSS3 (Custom variables, Grid, Flexbox)
- Vanilla JavaScript (no framework overhead)
- Three.js for 3D visualization

**Backend:**
- Flask (Python web framework)
- Subprocess for CLI integration
- Temporary file handling

**Why This Stack?**
- ✓ Cross-platform (works everywhere)
- ✓ No installation complexity
- ✓ Fast and lightweight
- ✓ Easy to maintain
- ✓ Professional appearance

## Design Philosophy

**Clean & Professional**
- No clutter, clear hierarchy
- Technical aesthetic (engineering tool)
- Dark mode optimized
- Monospace fonts for technical data

**User-Focused**
- Step-by-step workflow
- Visual feedback at every step
- Clear error messages
- Helpful hints

**Robotics Identity**
- Orange accent (energy, precision)
- Industrial feel
- Technical precision
- FRC team appropriate

## Browser Experience

```
Opening http://localhost:5000...

┌─────────────────────────────────────────────────────┐
│  [←] [→] [⟳]  localhost:5000              [ ][ ][×]│
├─────────────────────────────────────────────────────┤
│                                                     │
│                                                     │
│   [Clean, modern interface loads immediately]      │
│                                                     │
│   No external dependencies loading...              │
│   No slow startup...                               │
│   Just clean, fast, professional GUI               │
│                                                     │
│                                                     │
└─────────────────────────────────────────────────────┘
```

Fast loading:
- ✓ Minimal dependencies (only Three.js CDN)
- ✓ No heavy frameworks
- ✓ Inline CSS (no extra requests)
- ✓ Instant UI response

## Summary

The GUI provides a **professional, easy-to-use interface** that:

✅ Makes complex CAM operations simple
✅ Provides visual feedback at every step
✅ Shows real-time 3D preview
✅ Handles errors gracefully
✅ Works cross-platform
✅ Looks professional for FRC teams

**From DXF to G-code in 4 easy steps!** 🚀
