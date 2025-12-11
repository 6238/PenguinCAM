# PenguinCAM Quick Reference

**Student & Mentor Cheat Sheet - FRC Team 6238**

---

## 🚀 Quick Start (3 Steps)

### 1. Send Part from OnShape ⭐

**Easiest Method - One Click:**
1. Open your Part Studio in OnShape
2. **Right-click the part** in the feature tree (left sidebar)
3. Click **"Send to PenguinCAM"** from the menu
4. Part opens automatically in PenguinCAM!

**First Time Only:** You'll sign in with your @popcornpenguins.com Google account

**Alternative:** Manual DXF upload (see below)

---

### 2. Orient Your Part (Setup Mode)

After import, you'll see a **2D top-down view** of your part:

1. **Check orientation** - Is it rotated how you want?
2. Click **"Rotate 90° CW"** to match your stock material
3. **Origin is always bottom-left** (X→ Y↑)
   - Like 3D printer slicers or laser cutters
   - No need to pick a corner!

**Tip:** Orient your part so cutting direction matches grain or longest dimension

---

### 3. Generate & Download

1. Click **"Generate G-code"** 
2. **Review 3D preview** - Rotate to check toolpaths
3. **Use scrubber** to step through each cut
4. Click **"Download G-code"** OR **"Save to Google Drive"**

Done! 🎉

---

## 🔧 CNC Machine Setup

### Before You Start

**Material:**
- Clamp material flat to sacrifice board
- No gaps between material and sacrifice board
- Material must be fully supported

**Tools Needed:**
- Endmill (usually 1/8" or 4mm)
- Edge finder or piece of paper for zeroing
- Calipers to verify material thickness

---

### X & Y Zeroing

**Set origin at LOWER-LEFT corner of your material:**

1. **Jog** tool to lower-left corner of material
2. Use edge finder or "paper method":
   - Lower tool until it barely touches edge
   - Move along edge to find exact corner
3. **Set X=0 Y=0** in your CNC controller

**Why lower-left?**
- Matches OnShape coordinate system
- All toolpaths assume origin here
- X increases to the right, Y increases up

---

### Z Zeroing

⚠️ **CRITICAL: Z=0 is at the SACRIFICE BOARD (bottom), NOT the material top!**

**Setup:**
1. **Remove material** temporarily
2. **Jog tool down** to sacrifice board surface
3. Use paper method:
   - Place paper on sacrifice board
   - Lower tool until it pinches paper
   - Remove paper (adds ~0.003" - negligible)
4. **Set Z=0** at sacrifice board surface
5. **Replace material** on top of sacrifice board

**Why sacrifice board?**
- ✅ Guaranteed cut-through (0.02" overcut built in)
- ✅ Same zero point for all jobs
- ✅ No math when material thickness changes

See [Z_COORDINATE_SYSTEM.md](Z_COORDINATE_SYSTEM.md) for detailed explanation.

---

## 📐 Default Settings

**Material:**
- Thickness: **0.25"** (1/4" aluminum)
- Change in PenguinCAM if using different thickness

**Tool:**
- Diameter: **0.157"** (4mm endmill)
- Also works: 1/8" (0.125"), 1/4" (0.250")

**Feeds:**
- Cutting: **30 IPM**
- Plunging: **10 IPM**
- Adjust for your material and machine

**Tabs:**
- Count: **4** (evenly spaced)
- Width: **0.25"**
- Height: **0.03"** (material left uncut)
- You'll break these off after machining

---

## 🕳️ What PenguinCAM Knows

### Holes (Automatic Detection)

**#10 Screw Holes (~0.19" diameter):**
- Center drill operation (fast!)
- Perfect for #10 screws

**1.125" Bearing Holes:**
- Helical bore from center
- Exact size for VEX bearings

**Other Circles:**
- Milled as circular pockets
- Any non-standard size

### Pockets

**Inner closed shapes:**
- Full-depth plunge and cut
- Automatically offset for tool width

### Perimeter

**Outer boundary:**
- Cut with holding tabs
- Offset for correct final size
- Tabs only on straight sections (not curves!)

---

## ⚙️ Common Settings to Adjust

In PenguinCAM web interface:

**Material Thickness:**
- Measure with calipers!
- Don't guess - accuracy matters
- Common: 0.125" (1/8"), 0.25" (1/4"), 0.5" (1/2")

**Tool Diameter:**
- Check your actual endmill
- Common: 4mm (0.157"), 1/8" (0.125"), 1/4" (0.250")
- Wrong diameter = wrong part size!

**Number of Tabs:**
- Small parts: 3-4 tabs
- Large parts: 6-8 tabs
- More tabs = more secure but more cleanup

---

## 🎯 Design Tips for OnShape

### For Best Results:

**✅ Do:**
- Design parts as flat plates
- Use standard hole sizes when possible:
  - 0.19" for #10 screws
  - 1.125" for bearings
- Make perimeter a closed loop (no gaps!)
- Put pockets fully inside the perimeter

**❌ Avoid:**
- 3D features (PenguinCAM only processes top face)
- Open paths (must be closed shapes)
- Overlapping geometry
- Tiny features smaller than tool diameter

### Hole Sizes Quick Reference:

| Fastener | Clearance Hole | Tap Hole |
|----------|----------------|----------|
| #10 screw | 0.19" | 0.159" |
| 1/4-20 bolt | 0.257" | 0.201" |
| 1.125" bearing | 1.125" | - |
| M3 screw | 0.125" (3.2mm) | 0.098" (2.5mm) |

---

## 💾 Saving Your Work

### Download G-code (Always Works)

1. Click **"Download G-code"**
2. File saves to your computer
3. Load onto CNC via USB/network

### Save to Google Drive (Recommended)

1. Click **"Save to Google Drive"**
2. Uploads to: **Shared drives → Popcorn Penguins → CNC → G-code**
3. Everyone on team can access
4. Files stay organized
5. Survives when students graduate

---

## 🔍 Checking Your G-code

### In PenguinCAM (Before Download):

**3D Preview:**
- Rotate view with mouse drag
- Zoom with scroll wheel
- Look for:
  - ✅ All holes are cut
  - ✅ Pockets are milled
  - ✅ Perimeter has tabs
  - ✅ Nothing looks wrong

**Settings Summary:**
- Check material thickness is correct
- Verify tool diameter matches your endmill
- Note number of tabs

### On CNC (Before Running):

**Dry Run:**
1. Load G-code
2. **Turn spindle OFF**
3. Run program in "single block" or slow mode
4. Watch tool path - does it look right?
5. Check clearances - will tool hit clamps?

**Then:**
1. Return to start
2. Turn spindle ON
3. Run program for real!

---

## 🚨 Safety Checklist

**Before Running G-code:**

- [ ] Correct tool installed (match diameter in PenguinCAM)
- [ ] Material securely clamped
- [ ] Sacrifice board under material
- [ ] X/Y/Z zeros set correctly
- [ ] Dry run completed successfully
- [ ] Clamps won't interfere with tool path
- [ ] Dust collection running
- [ ] Safety glasses on
- [ ] Know where emergency stop is!

**While Running:**
- Stay nearby and watch the machine
- Be ready to hit emergency stop
- Never reach into cutting area
- Don't leave machine unattended

**After Machining:**
- Break off tabs carefully (pliers help)
- Deburr edges with file or sandpaper
- Check dimensions with calipers
- Celebrate! 🎉

---

## ❓ Common Issues & Fixes

### "Part is wrong size"

**Check:**
- Did you specify correct tool diameter in PenguinCAM?
- Is your actual endmill the size you think it is?
- Did you measure material thickness accurately?

### "Holes didn't cut through"

**Check:**
- Z=0 set at sacrifice board (not material top)?
- Sacrifice board gap-free under material?
- Material thickness entered correctly?

### "Part moved during cutting"

**Fix:**
- More clamps! Material must not move at all
- Reduce feed rate for better stability
- Add more tabs (6-8 for large parts)

### "Tabs are hard to break"

**Normal!** That's good - they held the part.

**Tips:**
- Use pliers to twist tabs off
- Score tab with utility knife first
- File/sand remaining material flush

### "Tool hit my clamps!"

**Prevention:**
- Always dry run first
- Position clamps outside tool path
- Check 3D preview for clearances
- Use low-profile clamps if needed

---

## 📞 Getting Help

**For PenguinCAM Problems:**
- Check this guide first
- Ask a mentor
- Report bugs: [GitHub Issues]

**For CNC Machine Problems:**
- Emergency: Hit E-stop immediately
- Ask mentor or experienced student
- Check machine manual
- Don't guess - ask for help!

**For Design Questions:**
- Review OnShape part - is it 2D?
- Check hole sizes match table above
- Ask mentor for CAD review

---

## 🎓 Best Practices

### For Students:

1. **Always dry run first** - catch mistakes before breaking tools
2. **Measure material thickness** - don't guess!
3. **Check tool diameter** - wrong tool = wrong size part
4. **Save to Drive** - helps whole team
5. **Stay safe** - machines are powerful and deserve respect

### For Mentors:

1. **Review student G-code** before first run
2. **Verify zeroing procedure** - most common mistake
3. **Start conservative** - slow feed rates until proven
4. **Keep spare endmills** - they break, especially while learning
5. **Celebrate successes** - first successful part is a big deal!

---

## 🔗 More Information

**Documentation:**
- [Tool Compensation Guide](TOOL_COMPENSATION_GUIDE.md) - How offsets work
- [Z-Coordinate System](Z_COORDINATE_SYSTEM.md) - Detailed zeroing guide
- [Deployment Guide](DEPLOYMENT_GUIDE.md) - For mentors setting up PenguinCAM
- [Roadmap](../ROADMAP.md) - Upcoming features

**Questions?**
- Ask your mentor
- Check team documentation
- GitHub: [Your repo link]

---

## 📤 Alternative: Manual DXF Upload

**If OnShape extension isn't available:**

1. **In OnShape:**
   - Right-click the face you want to machine
   - Export → DXF
   - Save the file

2. **In PenguinCAM:**
   - Go to https://penguincam.popcornpenguins.com
   - Sign in with @popcornpenguins.com account
   - Drag & drop DXF file (or click to browse)
   
3. **Continue as normal:**
   - Orient part in Setup Mode
   - Generate G-code
   - Download or save to Drive

**Same result, just an extra step!**

---

**Go Popcorn Penguins! 🍿🐧**

*Stay safe, machine smart, and build awesome robots!*
