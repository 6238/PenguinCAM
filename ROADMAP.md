# PenguinCAM Roadmap

**FRC Team 6238 Popcorn Penguins**  
CAM Post-Processor for Onshape → G-code workflow

---

## ✅ Current Status

PenguinCAM is **deployed and production-ready** at https://penguincam.popcornpenguins.com

**Core features working:**
- ✅ **Onshape one-click integration** - open Onshape App → Send to PenguinCAM
- ✅ Onshape OAuth integration with DXF export
- ✅ Automatic top face detection
- ✅ DXF → G-code post-processing
- ✅ Google Drive integration (uploads to shared drive)
- ✅ **Part orientation system** - Rotate in 90° increments, fixed bottom-left origin
- ✅ **2D Setup View** - Visualize part before generating toolpaths
- ✅ **3D toolpath visualization** - Interactive preview with tool animation
- ✅ Interactive scrubber to step through toolpaths
- ✅ Hole detection
- ✅ Non-standard holes milled as circular pockets
- ✅ Smart tab placement and automatic removal
- ✅ Tubing support - makes square ends and mirror-image pattern in opposing faces
- ✅ Tool compensation
- ✅ Multiple perimeter passes for thick materials
- ✅ Alerts users to unmillable features

**Preferred workflow:** One-click from Onshape (manual DXF upload also available)

---

## 🎓 Ready for FRC use!

PenguinCAM is ready for real-world use:
- Students can export parts from Onshape with one click
- Part orientation system matches 3D slicer/laser cutter workflows
- Visual preview before committing to G-code
- Direct save to team Google Drive

---

## 🚀 Future Enhancements (in no particular order)

### #1: Support multiple parts in a single job

**Priority:** Medium  
**Effort:** Medium-High

#### **Layout multiple instances of the same part on a single piece of stock**
#### **Allow for multiple parts to be cut on one piece of stock in the same job**

### #2: Per-team branding

**Priority:** Low
**Effort:** Medium

### #3: Support other cloud services for program storage

**Priority:** Low
**Effort:** Medium

### #4: Support G41/G42 tool compensation

**Priority:** Low
**Effort:** Medium

#### Would allow for more precise finished products (especially bearing holes) which are a pain point for many teams, as 1.125 ends up too small to use in many cases

### #5: Support 2.5D designs

**Priority:** Medium
**Effort:** High

#### Many teams prefer to have partial-depth pockets for weight savings or for recessing of bolt heads.
#### Our DXF intermediate format supports the concept of layers which would be used for this
#### But Onshape can not generate a multi-layer DXF file at this time, so we'd need to do it for them via part analysis

---

## 💡 Ideas for Consideration

*(Not committed to roadmap yet, but worth exploring)*

- Collision detection for tool holder
- Batch processing multiple DXFs
- G-code optimization (minimize tool changes)
- Export simulation as video/animated GIF
- Integration with other CAD platforms (Fusion 360, Inventor)
- Library of CNC machines

---

## 🤝 Contributing

PenguinCAM was built for FRC Team 6238 but is open for other teams to use and improve!

If you're interested in contributing:
1. Open an issue to discuss your idea
2. Fork the repo and make your changes
3. Submit a pull request

Questions? Contact: Josh Sirota <penguincam@popcornpenguins.com>

---

**Last Updated:** January 2026
**Maintained by:** FRC Team 6238 Popcorn Penguins
