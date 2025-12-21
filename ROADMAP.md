# PenguinCAM Roadmap

**FRC Team 6238 Popcorn Penguins**  
CAM Post-Processor for OnShape → G-code workflow

---

## ✅ Current Status

PenguinCAM is **deployed and production-ready** at https://penguincam.popcornpenguins.com

**Core features working:**
- ✅ **OnShape one-click integration** - Right-click "Applications" in OnShape → Send to PenguinCAM
- ✅ OnShape OAuth integration with DXF export
- ✅ Automatic top face detection
- ✅ DXF → G-code post-processing
- ✅ Google Workspace authentication (domain restriction)
- ✅ Google Drive integration (uploads to shared drive)
- ✅ **Part orientation system** - Rotate in 90° increments, fixed bottom-left origin
- ✅ **2D Setup View** - Visualize part before generating toolpaths
- ✅ **3D toolpath visualization** - Interactive preview with tool animation
- ✅ Interactive scrubber to step through toolpaths
- ✅ Hole detection (#10 screws, 1.125" bearings)
- ✅ Non-standard holes milled as circular pockets
- ✅ Smart tab placement
- ✅ Tool compensation

**Preferred workflow:** One-click from OnShape (manual DXF upload also available)

---

## 🎓 Ready for Student Testing

PenguinCAM is ready for real-world use:
- Students can export parts from OnShape with one click
- Part orientation system matches 3D slicer/laser cutter workflows
- Visual preview before committing to G-code
- Direct save to team Google Drive

**Next:** Test with actual FRC parts and compare against Fusion 360 CAM

---

## 🚀 Future Enhancements

### #1: Tube Mode for Aluminum Extrusions

**Priority:** High  
**Effort:** High

Add support for machining 1×1" and 1×2" aluminum tube extrusions with multi-face operations.

#### **Requirements:**
- Process 3D models (not just flat DXFs)
- Handle 4-face machining with pauses between faces
- Squaring operations on both ends
- Face-specific geometry (different features per side)
- Length cutting to final dimension

#### **Workflow:**
1. Import 3D tube model from OnShape
2. Extract geometry for each face (top, bottom, left, right)
3. Generate facing operations to square ends
4. Generate features for each face (holes, slots)
5. Insert M0 pauses between face rotations
6. G-code includes instructions for manual tube rotation

#### **Challenges:**
- Fixture limits (can't cut full depth)
- Reference surface creation (first face squared)
- Coordinate system per face
- User instructions for rotation sequence

**Impact:** Covers ~30% of FRC machining needs (plates + tubes = ~90% coverage)

---

### #2: Support multiple parts in a single job

**Priority:** Medium  
**Effort:** Medium-High

#### **Layout multiple instances of the same part on a single piece of stock**
#### **Allow for multiple parts to be cut on one piece of stock in the same job**

---

## 💡 Ideas for Consideration

*(Not committed to roadmap yet, but worth exploring)*

- Collision detection for tool holder
- Batch processing multiple DXFs
- G-code optimization (reduce rapids, minimize tool changes)
- Export simulation as video/animated GIF
- Integration with other CAD platforms (Fusion 360, Inventor)

---

## 🤝 Contributing

PenguinCAM was built for FRC Team 6238 but is open for other teams to use and improve!

If you're interested in contributing:
1. Open an issue to discuss your idea
2. Fork the repo and make your changes
3. Submit a pull request

Questions? Contact: [your contact info here]

---

## 📜 License

[Add your license here]

---

**Last Updated:** December 2025  
**Maintained by:** FRC Team 6238 Popcorn Penguins
