# PenguinCAM Roadmap

**FRC Team 6238 Popcorn Penguins**  
CAM Post-Processor for OnShape → G-code workflow

---

## ✅ Current Status

PenguinCAM is **deployed and functional** at https://penguincam.popcornpenguins.com

**Core features working:**
- ✅ OnShape OAuth integration with DXF export
- ✅ Automatic top face detection
- ✅ DXF → G-code post-processing
- ✅ Google Workspace authentication (domain restriction)
- ✅ Google Drive integration (uploads to shared drive)
- ✅ 3D toolpath visualization
- ✅ Hole detection (#10 screws, 1.125" bearings)
- ✅ Non-standard holes milled as circular pockets
- ✅ Smart tab placement
- ✅ Tool compensation

---

## ⏳ Blocked/Waiting

### OnShape Extension UI Integration
**Status:** Waiting on OnShape support ticket

The OnShape browser extension is configured but not appearing in the UI. We've opened a support ticket with OnShape to resolve visibility issues with extension configuration.

**Workaround:** Direct API integration works perfectly - users can process parts by providing OnShape URLs.

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

### #2: Origin Selection & Rotation Control

**Priority:** Medium  
**Effort:** Medium-High

**Goal:** User-controlled part positioning and orientation, similar to laser cutter alignment software.

#### **Current Behavior:**
- Origin automatically set to lower-left corner of bounding box
- No rotation control

#### **Planned Features:**

**🎯 User-Controlled Origin**
- Interactive 2D DXF view before toolpath generation
- Click to set origin point anywhere on the part
- Visual indicators for current origin
- Grid/ruler overlay for precise placement

**🔄 90° Rotation**
- Rotate part in 90° increments
- Match physical stock orientation (N-S vs E-W mounting)
- Preview rotation before applying

**🖼️ New UI Component: 2D Alignment View**
- Step inserted before 3D preview
- Shows DXF geometry in 2D
- Interactive controls for origin and rotation
- "Apply" button to generate toolpaths with user settings

**Benefits:**
- Better material utilization
- Flexibility for different stock orientations
- Match physical CNC setup exactly
- Optimize grain direction for wood/composites

---

## 💡 Ideas for Consideration

*(Not committed to roadmap yet, but worth exploring)*

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