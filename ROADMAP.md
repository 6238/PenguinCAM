# PenguinCAM Roadmap

**FRC Team 6238 Popcorn Penguins**  
CAM Post-Processor for OnShape → G-code workflow

---

## ✅ Current Status

PenguinCAM is **deployed and production-ready** at https://penguincam.popcornpenguins.com

**Core features working:**
- ✅ **OnShape one-click integration** - Right-click in OnShape → Send to PenguinCAM
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

### #1: G-code Validation & Testing

**Priority:** High  
**Effort:** Medium-High

#### **A. Compare vs Fusion 360 CAM**
- Analyze Fusion 360 CAM output for identical inputs
- Evaluate PenguinCAM approach vs Fusion approach
- Decision: Keep current logic or match Fusion exactly
- If beneficial, reverse-engineer Fusion's toolpath generation

#### **B. Automated Testing & Regression Detection**
- ✅ Unit tests with known DXF fixtures
- ✅ Expected G-code outputs for comparison
- 🔄 CI/CD integration to run tests automatically
- 🚨 Alerts when toolpaths change unexpectedly

**Benefits:**
- Confidence in generated G-code
- Catch bugs before they reach the CNC
- Safe refactoring and improvements
- Quality assurance for new features

---

## 💡 Ideas for Consideration

*(Not committed to roadmap yet, but worth exploring)*

- Tube support, beyond flat plates
- Multi-tool support (different endmills for roughing vs finishing)
- Material library with recommended feeds/speeds
- Collision detection for tool holder
- Support for more hole standards beyond #10 and 1.125"
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
