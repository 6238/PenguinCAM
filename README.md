# BionicsCAM 🦝

**Onshape-to-CNC CAM for FRC teams who would rather build robots than wrestle CAM software.**

BionicsCAM is a browser-based CNC workflow for flat FRC parts. It can import DXFs manually, pull selected faces from Onshape, generate toolpaths, preview G-code, and export ready-to-run NC files.

It started as a fork/rebrand of **PenguinCAM by FRC Team 6238**, then wandered into the trash can and came back with Onshape integration, auto-nesting, Vercel deployment, and a suspicious number of raccoon jokes.

**Live app:** <https://bionicscam.vercel.app>

🔗 **Demo video:**  
[![Demo video](https://img.youtube.com/vi/gFReFDz-_LI/0.jpg)](https://youtu.be/zPZCTVh2n2Q)

---

## What is BionicsCAM?

BionicsCAM turns flat CAD geometry into CNC-ready G-code with a workflow that feels closer to a slicer or laser cutter than traditional CAM.

Typical workflow:

1. Design a flat part in Onshape.
2. Select one large flat face per part.
3. Send it to BionicsCAM.
4. Check the setup preview.
5. Preview the generated G-code.
6. Download the NC file and make chips.

Manual DXF upload is also supported, including multi-part upload and auto-nesting.

---

## Why does this exist?

Because sometimes your team needs a bracket **now**, the normal CAM workflow is doing tax paperwork in a trench coat, and the router is just sitting there asking for G-code.

BionicsCAM is meant for students and mentors who need a practical path from CAD to CNC without making every new student become a feeds-and-speeds wizard overnight.

The raccoon philosophy:

> Make the safe path easy, make the dangerous path obvious, and never let the overlay raccoon back into the G-code preview.

### Origin story

First thing's first: huge thanks to **FRC Team 6238, Popcorn Penguins**, for PenguinCAM.

BionicsCAM started late at night when our team needed a part immediately and the hosted PenguinCAM instance was unavailable. We got a local version running, then the project spiraled into a Vercel-hosted app with Onshape integration, auto-nesting, and enough debugging chaos to accidentally create a raccoon-themed engineering religion.

The goal is simple: make it easier for the whole team to go from CAD to router-ready G-code without forcing every student to become a CAM expert on day one.

---

## Features

### Onshape integration

- Runs inside the Onshape right panel.
- Uses OAuth by default so each user imports with their own Onshape permissions.
- Select one flat face per part and import directly.
- Supports selected multi-part import.
- Avoids accidentally exporting the whole Part Studio unless explicitly intended.

### Manual DXF upload

- Single DXF upload.
- Multi-DXF upload.
- Quantity support for multi-upload.
- Works even when the Onshape API raccoon is having a day.

### Built for FRC

- Automatic hole detection.
- Circular holes are preserved from CAD geometry.
- Supports common screw holes, bearing holes, and custom sizes.
- Helical entry and spiral clearing where applicable.
- Smart perimeter cutting for flat plates.
- Designed for student-friendly workflow, not enterprise CAM wizardry.

### Auto-nesting

- Places multiple parts onto stock.
- Uses simple, reliable rectangular packing.
- Supports quantity expansion.
- Adds clearance between parts.
- Generates one combined NC file.

Auto-nesting V1 is intentionally simple. It does **not** do full polygon nesting, genetic algorithms, simulated annealing, AI magic, or raccoon divination.

### G-code generation

- Hole detection.
- Perimeter cuts.
- Tabs.
- Tool diameter compensation.
- 2D workflow for flat plates.
- 2.5D support where applicable.
- One combined file for nested jobs.

### Preview tools

- DXF setup preview.
- G-code preview.
- Stock outline.
- Multi-part preview.
- Overlay raccoon suppression when switching to G-code preview.

---

## Quick Start

### Option 1: Onshape import

1. Open a Part Studio in Onshape.
2. Open the BionicsCAM panel.
3. Select one large flat face per part.
4. Click import.
5. Review the setup preview.
6. Click **Preview G-code**.
7. If it looks sane, click **Generate Program**.

If Onshape import fails because of permissions, make sure the logged-in user can access the document. In OAuth mode, BionicsCAM uses that user's Onshape access.

### Option 2: Manual DXF upload

1. Export DXFs from Onshape or another CAD program.
2. Open BionicsCAM.
3. Upload one or more DXF files.
4. Set quantity, stock size, gap, and other parameters.
5. Preview the setup.
6. Preview G-code.
7. Generate the NC file.

Manual upload is the emergency exit when the Onshape raccoon eats the API tokens.

---

## Recommended Student Workflow

1. Make the part in CAD.
2. Keep it flat and machinable.
3. Select the biggest flat face.
4. Send it to BionicsCAM.
5. Look at the preview before generating.
6. Do not run mystery G-code because “it probably works.”
7. Ask a mentor before sacrificing aluminum to the router gods.

---

## Configuration

BionicsCAM can use team configuration files for machine defaults, feeds, speeds, tool sizes, and other settings.

Common local config files:

```text
machine_config.json
auth_config.json
drive_config.json
```

For hosted/team usage, BionicsCAM can also look for a PenguinCAM-style config file in Onshape when enabled.

---

## Environment Variables

Common deployment variables:

```text
BASE_URL
FLASK_SECURITY_KEY
ONSHAPE_CLIENT_ID
ONSHAPE_CLIENT_SECRET
```

Optional API-key variables, only if API-key backend mode is intentionally enabled:

```text
ONSHAPE_ACCESS_KEY
ONSHAPE_SECRET_KEY
ONSHAPE_BACKEND_AUTH_MODE=api_key
```

Default recommended mode for multi-user classroom/team use:

```text
ONSHAPE_BACKEND_AUTH_MODE=oauth
```

OAuth mode means each user spends their own Onshape API requests and uses their own document permissions. Fewer permission raccoons, more personal responsibility.

---

## Local Development

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Run the app:

```bash
python3 frc_cam_gui_app.py
```

Or, depending on your setup:

```bash
python3 app.py
```

Then open the local URL printed in the terminal.

### Basic checks

Run these before committing changes:

```bash
python3 -m py_compile app.py frc_cam_gui_app.py frc_cam_postprocessor.py onshape_integration.py
node --check static/app.js
node --check static/onshape_panel.js
```

If the repo has been reorganized into folders, adjust paths accordingly. The raccoon cannot read your mind, but `ls` can help.

---

## Testing Checklist

Before calling a build “good,” test these:

- Single manual DXF upload.
- Multi-DXF upload.
- Multi-DXF quantity.
- Setup DXF preview shows all parts.
- G-code preview shows all parts.
- DXF overlay disappears in G-code preview.
- Generate Program produces one combined NC file.
- Onshape selected single-part import.
- Onshape selected multi-part import.
- 2D runtime is reasonable.
- 2.5D does not try to cut every line like a spaghetti machine.

If a change touches nesting, preview, Onshape, or post-processing, test twice. That is where the raccoons live.

---

## Repository Map

Core files in the current flat layout:

```text
app.py                         Vercel/Flask entrypoint
frc_cam_gui_app.py             Main Flask web app
frc_cam_postprocessor.py       CAM and G-code generation
onshape_integration.py         Onshape API/OAuth/export logic
penguincam_auth.py             Auth helpers
google_drive_integration.py    Optional Drive integration
metrics.py                     Metrics/logging helpers
static/                        Frontend JS/CSS/assets
templates/                     HTML templates
docs/                          Documentation
requirements.txt               Python dependencies
vercel.json                    Vercel deployment config
```

If this repo gets reorganized later, please leave a `REORG_MAP.md`, because future-us deserves mercy.

---

## Deployment

BionicsCAM is currently deployed on Vercel.

Typical deploy flow:

```bash
git add -A
git commit -m "Describe the change"
git push origin main
```

Vercel redeploys from `main`.

Useful production debug endpoints:

```text
/api/onshape-auth-mode
/api/onshape-auth-test
```

These should not expose secrets. They are for confirming whether the app is using OAuth, API keys, and whether the backend can talk to Onshape.

---

## Troubleshooting

### Onshape import says permission denied

The selected user or backend auth mode cannot access the document.

In OAuth mode, make sure the logged-in Onshape user can open the document.

In API-key mode, make sure the API-key account can access the document.

### Onshape says API limit exceeded

Stop clicking import like it owes you money.

Check whether BionicsCAM is using OAuth or API-key mode. Each mode may burn a different account's quota.

### Only one part appears in DXF preview

The preview path is probably only rendering the first DXF. Check multi-DXF setup preview logic.

### G-code shows all parts but DXF preview does not

Backend is probably fine. Frontend preview parser/rendering is the suspicious raccoon.

### Parts overlap after nesting

Check bounding boxes, rotation, clearance, and whether preview and G-code placement use the same math.

### Vercel says 500

Read the function logs. The useful part is usually the line before the traceback or the last Onshape API response.

---

## Credits

BionicsCAM is based on **PenguinCAM by FRC Team 6238, Popcorn Penguins**.

Huge thank-you to Team 6238 for releasing PenguinCAM under the MIT License. BionicsCAM/RaccoonCAM would not exist without that foundation.

Additional BionicsCAM work by Srihaas Mynampati, Chris Johnson, and the Big Man Himself, Blake Borque, with extensive assistance from **ahem** AI tools and at least one metaphorical raccoon.

---

## License

This project includes code derived from PenguinCAM, which is licensed under the MIT License.

BionicsCAM is also distributed under the MIT License unless stated otherwise.

Keep the original copyright and MIT license notices when redistributing or publishing modified versions.

The license file should be boring. The README may contain raccoons.

---

## Name Notes

This project may be referred to as:

```text
BionicsCAM   current project/app name
RaccoonCAM   chaotic spiritual successor / possible app-store name
PenguinCAM   original upstream inspiration by Team 6238
```

If publishing publicly, be clear that RaccoonCAM/BionicsCAM is a modified fork and is not officially endorsed by Team 6238 unless they say so.

---

## What's With All The Raccoons?

Raccoons **are not** my favorite animal. This started as a debugging joke.

When BionicsCAM first started working, it was tested by a teammate, Declan Murphy. Manual DXF upload worked, but the Onshape import path returned a painfully vague error:

```json
{
  "error": "No parts could be exported from this document",
  "message": "BionicsCAM could not resolve/export the selected Onshape faces. Try selecting one large flat face per part."
}
```

Very helpful. Very polite. Completely useless.

Anyways, I spent about 3 hours of my own time tryna debug it, to no avail. I asked Claude, it did nothing to help. I asked ChangGPT (Deepseek), still nada. I asked ChatGPT, nope. I gave up, accepted damnation, and looked at the Vercel logs. It looked like this:

```json
{
  "content_type": "application/json",
  "face_id": "JPK",
  "response_preview": "{\n \"message\" : \"API limit exceeded\",\n \"moreInfoUrl\" : \"\",\n \"status\" : 402,\n \"code\" : 402\n}",
  "route": "workspace",
  "selector_key": "faceIds",
  "status_code": 402
}
```

Much clearer.

I immediately understood what happened and I pasted the logs to GPT-man. It was like "OH. We finally caught the raccoon on camera." That immediately became a metaphor for us. Since then, “raccoon” has been the official term for any bug, weird API behavior, preview overlay zombie, or mysterious CNC gremlin.

---

## Final Warning

CNC routers are real machines. They do not care that the preview looked cute.

Always verify toolpaths, clamp material, set zeros correctly, and keep hands away from the danger zone.

If something looks wrong, stop. The raccoon can wait.
