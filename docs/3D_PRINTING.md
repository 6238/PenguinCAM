# 3D Printing: the print path (stage 1)

Developer guide for PenguinCAM's 3D print path as built on `feature/3d-print-stage1`.
The design it implements is the [design spec](superpowers/specs/2026-09-13-3d-print-slicing-design.md).
Deployment numbers live in [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md). The fixed assets
are described in [print/README.md](../print/README.md).

## 1. What stage 1 does

Stage 1 proves the slicing pipeline end to end. It slices one part, with one profile
set, through Orca Slicer's command line, on a wizard page of its own.

- **One fixed part.** `print/sample_part.stl`, an L bracket of 40 x 30 x 12 mm. Nothing
  is exported from Onshape yet.
- **One fixed profile set.** Three flattened Bambu Lab profiles in `print/profiles/`.
  Team configuration does not choose them yet.
- **Orca Slicer's command line.** A pinned release at a fixed path, run headless as a
  subprocess. No GUI, no display server.
- **A separate wizard page.** `/print` has the same look as the CNC wizard and none of
  its code. It shares the stylesheet, the session gate, the file token manager, the
  download route and the Drive route.

The student picks 3D Printing on the Setup screen, walks Setup, Parts, Layout and
Preview, and saves `sample_part.gcode.3mf`.

## 2. Architecture

```mermaid
flowchart TB
    A["Setup radio<br/>3D Printing"] --> B["/print page"]
    B --> C["GET /print/part<br/>names, bed, STL"]
    B --> D["POST /print-job"]
    B --> S["GET /print-job/id/events"]
    D --> P["print_jobs.py<br/>pool and job table"]
    S --> P
    P --> E["print_slicer.py"]
    E --> F["tools/orca/orca-slicer"]
    F --> G[("sample_part.gcode.3mf")]
    G --> H["file token manager"]
    H --> I["/download/token"]
    H --> J["/drive/upload/token"]
```

`print_routes.py` holds the blueprint. `print_jobs.py` holds the pool and the job table
and imports no Flask. `print_slicer.py` runs Orca and imports no Flask.
`frc_cam_gui_app.py` calls `init_print_routes(...)` once near the end of the file, after
the limiter and the temp folders exist, and hands over the session gate, the token
manager, the limiter, the upload and output folders, the template context, metrics and
the log function. `print_routes.py` never imports `frc_cam_gui_app`, so there is no
import cycle.

## 3. Orca Slicer, the fixed location

Orca is a core dependency, like the Python interpreter. It is never optional and never
configurable.

### The fixed path

The entry point is always `tools/orca/orca-slicer`, relative to the repository root.
`print_slicer.py` computes that path from its own file location. There is no environment
variable and no lookup on `PATH`.

### The install script

`scripts/install-orca.sh` is the only place the version and the checksums live. It pins
`ORCA_VERSION=2.4.2` and one SHA-256 per platform, and downloads from
`github.com/OrcaSlicer/OrcaSlicer`.

| Platform | `uname -s` / `uname -m` | Asset |
|----------|--------------------------|-------|
| Railway production, CI | Linux / x86_64 | `OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage` |
| Agent containers, Docker on the Mac | Linux / aarch64 | `OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.4.2.AppImage` |
| Mac development | Darwin / arm64 | `OrcaSlicer_Mac_universal_V2.4.2.dmg` |

Any other combination is refused with a message naming both values. A checksum mismatch
deletes the download and exits non-zero. The script is idempotent: if
`tools/orca/VERSION` already reads `<version> <arch>` and the wrapper is executable, it
exits without touching the network. The stamp carries the architecture, so a `tools/`
directory copied from a Mac into a Linux build is detected and replaced, not trusted.
Anything already at `tools/orca` that does not match is deleted before the download.

### The Linux wrapper

On Linux the AppImage is extracted with `--appimage-extract`, so no FUSE is needed, and
the script writes `tools/orca/orca-slicer` as a shell wrapper. The wrapper does not use
the AppImage's `AppRun`. AppRun execs `libexec/orca-slicer-env`, and that launcher probes
`ldconfig -p` and `/usr/lib` for the host's OpenGL and WebKit libraries, which fails in a
container without those packages. The wrapper sets what `AppRun` would set and execs the
binary directly:

- `APPDIR` pointing at `squashfs-root`.
- `LC_ALL=C`.
- `LD_LIBRARY_PATH` of `squashfs-root/lib/orca-runtime` and `squashfs-root/bin`, plus
  `tools/orca/hostlibs/usr/lib/<triplet>` and its `gstreamer-1.0` subdirectory when that
  directory exists.

All arguments pass through. On macOS the script mounts the disk image, copies
`OrcaSlicer.app` into `tools/orca/`, clears `com.apple.quarantine` with `xattr -dr`, and
writes the same style of wrapper pointing at `Contents/MacOS/OrcaSlicer`.

Either way the script finishes with a smoke test: it runs `orca-slicer --help` inside a
scratch directory and deletes it, because Orca drops a `result.json` in the working
directory on every run, `--help` included. Only then is `tools/orca/VERSION` written.

### The unprivileged `hostlibs` path

The AppImage bundles a handful of private libraries and takes the rest from the host.
After extraction the script runs `ldd` against the binary and every host library it has
unpacked so far, and collects the sonames that do not resolve. If nothing is missing it
stops there.

If libraries are missing and the script runs as root, it fails and tells the operator to
install the distro packages. If it is not root, it bootstraps them:

1. Fetch the Ubuntu noble `Contents-<arch>.gz` index and the `main` and `universe`
   `Packages.gz` files from the archive mirror for the architecture.
2. Map each missing soname to the package that ships `usr/lib/<triplet>/<soname>`,
   ignoring `-dev` and debug packages.
3. Read that package's pool filename and SHA-256 from the `Packages` index, download it,
   verify the checksum, and unpack it with `dpkg-deb -x` into `tools/orca/hostlibs/`.
4. Repeat the `ldd` check, up to twelve rounds, and fail if a round makes no progress.

The wrapper picks `hostlibs` up automatically. `dpkg-deb` is required for this path. In
the Docker image the distro packages are present, `ldd` is clean on the first pass, and
`hostlibs` is never created.

### The Docker image

The Dockerfile pins `python:3.11-slim-trixie`. The Linux AppImages are built on Ubuntu
24.04 and need glibc 2.38 or newer; Debian 13 has 2.41. The pin stops a future tag move
from changing the C library under Orca.

The image installs `ca-certificates` and `curl` plus Orca's runtime libraries, resolved
from the trixie package index and identical on amd64 and arm64:

```
libopengl0 libegl1 libgl1 libglx0 libglu1-mesa
libgtk-3-0t64 libgdk-pixbuf-2.0-0 libatk1.0-0t64 libcairo2 libcairo-gobject2
libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1
libglib2.0-0t64 libdbus-1-3
libgstreamer1.0-0 libgstreamer-plugins-base1.0-0
libwebkit2gtk-4.1-0 libjavascriptcoregtk-4.1-0 libsoup-3.0-0 libsecret-1-0
libwayland-client0 libwayland-egl1 libwayland-server0 libxkbcommon0
libx11-6 libxext6 libsm6 libice6
libstdc++6 libexpat1 liblzma5 libmspack0t64 zlib1g
```

The install script runs before the application code is copied, so a code change does not
re-download 130 MB. `tools/` is in `.dockerignore`, so the build's own install survives
the `COPY . .`. The build ends with an `ldd` check on
`tools/orca/squashfs-root/bin/orca-slicer` and fails if anything prints `not found`.

### Commands

```bash
make install        # Python dependencies and Orca
make install-orca   # Orca only; runs scripts/install-orca.sh
```

## 4. Profiles

### Why exported profiles do not work

Orca's vendor profiles are inheritance chains. A Bambu printer profile inherits from a
family profile, which inherits from a base, and each file holds only the keys it
overrides. The command line does not resolve `inherits`. It loads exactly the keys in the
files it is given and fills every other key with built-in defaults, silently. Slicing
with an unflattened X1 Carbon profile "succeeds" with a 200 mm bed, a filament density of
zero and no weight in the output.

The desktop app's export does not help. It writes a user preset's differences plus the
parent's name, not a resolved file. A printer profile marked `from: User` with no
`inherits` also fails Orca's printer-to-process compatibility check with return code -17.
That is why the flattened files set `from` to `system`.

### `scripts/flatten_orca_profiles.py`

The script reads the `BBL` vendor tree inside the installed Orca:
`tools/orca/squashfs-root/resources/profiles/BBL/` on Linux, or the equivalent under
`OrcaSlicer.app/Contents/Resources/` on macOS. For each output file it:

1. Follows `inherits` from the named profile to the root, refusing a loop.
2. Merges the chain child over parent.
3. Sets `inherits` to an empty string and `from` to `system`.
4. Drops `instantiation` and `setting_id`, which only mean something inside Orca's own
   preset database.
5. Writes JSON with `indent=4`, `sort_keys=True`, `ensure_ascii=False` and a trailing
   newline, so the checked-in files compare byte for byte.

| Output | Vendor subdirectory and profile |
|--------|---------------------------------|
| `print/profiles/printer.json` | `machine/Bambu Lab X1 Carbon 0.4 nozzle` |
| `print/profiles/filament.json` | `filament/Bambu PLA Basic @BBL X1C` |
| `print/profiles/process.json` | `process/0.20mm Standard @BBL X1C` |

Regenerate after an Orca upgrade, from the repository root:

```bash
uv run python scripts/flatten_orca_profiles.py
```

Orca's `version` key is a vendor profile version, not the application version, and is
not used.

### The two guarding tests

- `tests/test_print_slicer.py` asserts each checked-in profile has an empty `inherits`
  and carries the keys the pipeline depends on: `name`, `printable_area`,
  `printable_height`, `filament_density`, `layer_height`, and `compatible_printers` on
  the process and the filament.
- `tests/orca_integration_test.py` re-runs the flattening script against the installed
  Orca and fails if the output differs from the checked-in files, so an Orca upgrade
  forces a regeneration.

## 5. Sample part

`print/sample_part.stl` is an L bracket, 40 x 30 x 12 mm, a binary STL in millimetres.
`scripts/make_sample_part.py` generates it: the polygon (0,0) (40,0) (40,10) (10,10)
(10,30) (0,30) extruded 12 mm in Z, twenty triangles, one closed shell, outward normals.

Each cap is fanned from the reflex vertex (10,10) using only real polygon vertices. That
keeps every cap boundary edge equal to one footprint edge and leaves no T-junction, so
the shell is closed. The generator is deterministic and `tests/test_make_sample_part.py`
asserts the checked-in file equals its output byte for byte.

## 6. `print_slicer.py`

Flask-free. Importing the module calls `_require_binary()`, which raises `RuntimeError`
naming `make install` when `tools/orca/orca-slicer` is absent. That import guard is why
every test module that imports the app needs Orca installed, and why CI installs Orca
before `make test`.

### The command line

`build_command(stl_path, output_dir)` resolves both paths to absolute ones first, because
Orca runs with `cwd=output_dir` and would read a relative path against the wrong
directory. The first element is the absolute path of `tools/orca/orca-slicer`, the module
constant `ORCA_BIN`. It returns, verbatim:

```
<repo>/tools/orca/orca-slicer
    --datadir <output_dir>/datadir
    --load-settings "<repo>/print/profiles/printer.json;<repo>/print/profiles/process.json"
    --load-filaments <repo>/print/profiles/filament.json
    --arrange 1
    --slice 0
    --export-3mf <stem>.gcode.3mf
    --outputdir <output_dir>
    <stl_path>
```

`--arrange 1` centres the part on the plate, which matches the Layout drawing. `--slice 0`
slices every plate; the sample has one.

The pinned binary creates one missing level of `--outputdir` itself. It fails when the
parent is missing too: exit code 156, with a `boost::filesystem::create_directory` error.
`slice_stl` creates the directory anyway, because it also runs Orca with `cwd=output_dir`
and the working directory has to exist before the process starts.

### Isolation

The subprocess gets an allowlisted environment, not the server's. Only `PATH`, `HOME`,
`TMPDIR`, `LANG`, `USER` and `LOGNAME` are copied through, so tokens and client secrets
never reach Orca. On top of that `slice_stl` sets:

| Variable or flag | Value |
|------------------|-------|
| `XDG_CONFIG_HOME` | `<output_dir>/xdg-config` |
| `XDG_RUNTIME_DIR` | `<output_dir>/xdg-runtime`, mode 0700 |
| `LC_ALL` | `C` |
| `--datadir` | `<output_dir>/datadir` |

Every per-user write therefore lands in the job's own scratch directory, and two
concurrent slices never share one. Without `XDG_RUNTIME_DIR` Orca prints an `error:` line
on stderr and carries on, which is why stderr text is never used to judge success.

### Success and failure

Success is the exit code plus the presence of the archive. `SliceError` is raised when
the process cannot be started, times out, exits non-zero, leaves no archive, produces
something that is not a zip, or produces an archive with no `Metadata/plate_1.gcode`
entry. It carries two strings: `message`, one sentence fit for the student, and
`details`, the last twenty lines of Orca's output (or the archive listing) for the server
log. On a non-zero exit the exit code leads `details` and the tail is trimmed to nineteen
lines, so `details` stays twenty lines and the code is always visible. `details` never
reaches the browser.

### `SliceResult`

| Field | Source |
|-------|--------|
| `output_path` | `<output_dir>/<stem>.gcode.3mf` |
| `print_time_s` | `Metadata/slice_info.config`, `<metadata key="prediction" value="…"/>`, seconds |
| `filament_g` | same file, `<metadata key="weight" value="…"/>`, grams |
| `filament_m` | same file, the `used_m` attribute of the `<filament>` element |
| `layer_count` | `Metadata/plate_1.gcode`, header line `; total layer number: N` |
| `warnings` | `result.json` in the output directory, the `warning_message` of each sliced plate |

Only the first 4096 bytes of the G-code entry are decompressed: a real plate runs to
hundreds of megabytes and the header is the first few lines. A missing field is `None`
rather than an error, and the archive is still delivered. `SliceResult.summary()` returns
the four numbers plus the warnings as a plain dict, which is what the job record and the
browser see.

Orca also writes a loose `plate_1.gcode` and, on Linux, a `result.json` beside the
archive, so the output directory is scratch space and only the archive is kept.

### `part_info()`

`part_info()` returns the names and numbers the wizard shows before slicing: the part's
stem, file name, bounding box in millimetres and triangle count, computed by
`stl_bounds()` from the STL bytes; the printer name, bed size and printable height; and
the filament and process names with the layer height. Every value in an Orca profile is a
string. `printable_area` is a list of `"XxY"` corner strings such as `"256x256"` and
`printable_height` is a string like `"250"`, so `part_info()` parses them to numbers once.
`stl_bounds()` reads binary and ASCII STLs; a binary file is identified by its exact
length, never by a leading `solid`, because some tools write that word into binary files.

## 7. Job pool

`print_jobs.py` owns a job table in process memory and a `ThreadPoolExecutor`. No Flask,
no app imports.

| Constant | Value | Basis |
|----------|-------|-------|
| `MAX_RUNNING` | 1 | Orca processes at once. |
| `MAX_QUEUED` | 2 | Jobs waiting beyond the running ones. Beyond that, `submit()` raises `QueueFull` and the route answers 503, because queued work nobody will wait for is worse than an honest refusal. |
| `RECORD_TTL_S` | 3600 | A record expires one hour after its last update, read or not. |
| `WAIT_S` | 5.0 | Subscriber wake-up cadence when nothing changes. |
| `SWEEP_INTERVAL_S` | 600 | Expiry sweep cadence, the same as the token manager's. |

`MAX_RUNNING` is sized by the rule `min(vCPU count, memory / peak resident memory of one
slice)`, never below one. One slice of the sample part peaked at 93 MB resident and took
0.23 s on the aarch64 agent container. A 120 x 120 x 40 mm box took 0.6 s at 250 MB peak.
The production Railway plan's vCPU and memory allotment could not be read from the build
container, so the value stays at one until it is measured.

The job TTL and the token manager's file retention are independent clocks. Both are one
hour today, but nothing links them: the token manager's cleanup thread unlinks registered
files after their own retention period, and the pool's sweeper drops records after
`RECORD_TTL_S`.

### Records and states

A record holds `id`, `state`, `created`, `updated`, `token`, `summary`, `part` and
`error`. `get()` and `wait()` return a copy with `elapsed_s` added. States run `queued`,
`running`, then `done` or `failed`. Ids are `secrets.token_urlsafe(32)`, so knowing one is
the only credential needed to read its result, the same model the file token manager uses
for downloads.

### Subscribers

Each job has a `threading.Condition` sharing the table's lock. `wait(job_id, since_updated)`
returns at once if the record has moved on, otherwise waits up to five seconds. Any state
change notifies every subscriber. `follow(job_id)` is the generator behind the event
route: it yields `("state", record)` now, then on every change or five-second wake, then
one terminal `("done"|"failed", record)`, and returns. It also returns when the record
disappears. `expire()` pops the condition and deletes the record, then calls
`notify_all()` on the condition it still holds, so every waiter wakes up.

### The worker

`make_worker(...)` in `print_routes.py` builds the function the pool runs. It creates a
scratch directory under the upload folder, calls `slice_stl`, moves the archive to
`<output_folder>/print_<job_id>.gcode.3mf`, registers that path with the token manager
under the delivered name `sample_part.gcode.3mf`, and returns the token, the summary and
`part_info()`. If registering the token fails, the archive is unlinked: without a token
nothing could ever fetch or expire it. A `SliceError` becomes a `JobFailure`, so the
record ends `failed` with the student message and the details go only to the log. The
`finally` block removes the scratch directory and logs the `print_job` metrics event with
the outcome and the duration. The token manager's cleanup does not remove directories,
which is why the worker removes its own scratch space.

## 8. Routes

All five live in `print_routes.py` and are registered by `init_print_routes`.

| Route | Method | Gate | Rate limit | Responses |
|-------|--------|------|------------|-----------|
| `/print` | GET | app session | 200 per hour (app default) | 200 with the page; redirect to the validated `return` with `verify=1` when the gate fails |
| `/print/part` | GET | none | 200 per hour (app default) | 200 JSON from `part_info()`; with `?stl=1`, 200 with the STL bytes as `model/stl` |
| `/print-job` | POST | app session | 3 per minute | 202 `{job_id}`; 503 `{error}` when the queue is full; 401 from the gate; 429 over the limit |
| `/print-job/<id>` | GET | app session | 200 per hour (app default) | 200 with the record; 404 `{error}` for an unknown or expired id |
| `/print-job/<id>/events` | GET | app session | 200 per hour (app default) | 200 `text/event-stream`; 404 `{error}` for an unknown id |

The app's limiter is built with `default_limits=["200 per hour"]`, so every route carries
that budget. Nothing on the print path is exempt. Two error bodies are written for the
student, as the constants `BUSY_MESSAGE` and `EXPIRED_MESSAGE` in `print_routes.py`:

| Status | Body | Constant |
|--------|------|----------|
| 503 | `The slicer is busy, try again in a minute.` | `BUSY_MESSAGE` |
| 404 | `This slice has expired, go back and slice again.` | `EXPIRED_MESSAGE` |

`/print/part` is deliberately ungated: it serves only the sample part's metadata and STL,
neither of which is a secret.

`/print` reads `source` (`upload` or `onshape`, default `upload`) and `theme` (`dark` or
`light`, default `dark`), falling back to the default on anything else. When
`source=onshape` it sets `Content-Security-Policy: frame-ancestors https://*.onshape.com`
and drops `X-Frame-Options`, so the page can live in the Onshape panel.

The rate limit is added inside `init_print_routes` with
`limiter.limit("3 per minute")(print_job_submit)` rather than with a decorator, because
flask-limiter only evaluates a limit through the wrapper its decorator returns and the
limiter does not exist when the module is imported.

The limit keys on the remote address, like every existing limit. `ProxyFix` is configured
with `x_proto` and `x_host` but no `x_for`, so behind Railway's proxy every browser shares
one address and three per minute is a deployment-wide cap. The fourth slice in a minute
sees a 429 on Preview. Trusting the forwarded-for header is a separate change.

The 200 per hour default is deployment-wide for the same reason. The polling fallback
hits `/print-job/<id>` every 2 s, or 30 requests a minute, and every one of them spends
that budget. The fallback only runs when the event stream fails, so a normal slice uses a
handful of requests.

### The `return` parameter and `verify=1`

`return` is user-controlled, so `safe_return_path()` accepts only a same-origin path. It
must start with a single `/`; `//host` and `/\host` are rejected, as is any control
character, which Werkzeug would otherwise refuse with a 500 when it reached the `Location`
header. Anything rejected becomes `/app`.

When the session gate fails, `/print` redirects to the validated return with `verify=1`
appended. `_with_verify()` appends with `?` or `&` as needed and keeps any `#fragment` at
the end. The CNC page runs its verification on load, so the student lands on a page that
can verify them. The panel passes its own path and query as `return`, so the document,
workspace, element, server and theme parameters survive the round trip.

### The event stream

The response is a generator with `Content-Type: text/event-stream`,
`Cache-Control: no-cache` and `X-Accel-Buffering: no`. Each block is an event name and
one line of JSON, the same record the JSON route returns:

```
event: state
data: {"id": "…", "state": "running", "created": 1757740000.0, "updated": 1757740001.2, "token": null, "summary": null, "part": null, "error": null, "elapsed_s": 1.2}

```

The terminal block is `event: done` or `event: failed` with the full record, and then the
stream closes. The JSON route exists so the browser can poll when the stream fails and so
a developer can inspect a job with `curl`.

### Delivery

`/download/<token>` picks its MIME type by extension: `.3mf` downloads as
`application/octet-stream`, everything else stays `text/plain`. That one line in
`frc_cam_gui_app.py` and the `init_print_routes` call are the only changes the print path
makes to the Flask module. `/drive/upload/<token>` uploads by path and lets the Drive
client pick the type from the file name, so it needed no change.

### gunicorn

The Dockerfile's `CMD` and the `Procfile` carry the same line:

```
gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16
```

Threads are sixteen so several open event streams can sit beside ordinary requests.
Workers are pinned to one, and `--workers 1` is explicit because gunicorn otherwise reads
`WEB_CONCURRENCY`: the job table, the file token map and the limiter's memory storage are
all per process, and a second worker would see none of the first worker's jobs, tokens or
counters.

## 9. Frontend

### The two layouts

`templates/print_wizard.html` copies the CNC wizard's header, step bar and navigation
footer rather than sharing them, so the two pages can diverge. In the Onshape panel
(`source=onshape`) the steps show one at a time with a Next button and a row of summary
chips. In full-page mode (`source=upload`) `print_wizard.js` adds the `grid` class, all
four steps show at once with the current one highlighted, the chips stay hidden, and the
2.5D radio is hidden and disabled, as on the CNC page.

The page carries `window.PenguinCAM` with four fields: `source`, `theme`, `returnUrl` and
`driveEnabled`. `print_wizard.js` adds a fifth at run time, `getJobState()`, which returns
the last state seen and exists for the acceptance run.

### `static/print_wizard.js`

Owns step navigation, the summary chips and the three data calls. Named parts worth
knowing:

- `loadPart()` fetches `/print/part` once on load and fills Setup, Parts and Layout
  through `fillSetup()`, `fillParts()` and `fillLayoutInfo()`. Part info can arrive after
  the first `gotoStep()`, so it re-applies the Layout gate.
- `drawLayout()` draws the bed, a 50 mm grid and the part footprint centred on it, on a
  2D canvas, in theme colours read from CSS custom properties.
- `fitsBed()` and `tooBigMessage()` gate Layout: a part larger than the bed in any axis
  shows the error and leaves Next disabled, and `canLeave()` refuses to move past it.
- `gotoStep()` and `navigateTo()` drive the steps and the step bar. Leaving Preview calls
  `stopFollowing()`.
- `enterPreview()` submits the slice. It bumps a generation counter so a late response
  from a submit the page has moved on from is dropped.
- `followEvents()` opens an `EventSource` and handles the `state`, `done` and `failed`
  events. On an error before a terminal event it closes the stream and calls
  `startPolling()`, which polls `/print-job/<id>` every two seconds, so a proxy that
  cannot stream still finishes the job.
- `showDone()` fills the summary line, shows warnings in the notes box, enables the save
  button and calls `showMesh()`, which fetches `/print/part?stl=1` and hands the bytes to
  the viewer. A browser with no WebGL adds a note instead of losing the numbers.
- `errorForStatus()` maps 429 and 404 to the student messages and otherwise shows the
  server's `error` field.
- `setupFinalAction()`, `chooseAction()` and `performAction()` implement the split button,
  including the remembered last choice in `localStorage` and the Drive popup, following
  the CNC wizard's pattern in this file's own code.

Re-entering Preview on the same page load does not re-slice. If the job in hand is `done`
with a token, `enterPreview()` shows it again; if it is still running, it re-attaches to
the stream. The result is good for an hour and re-slicing would spend the three-a-minute
budget.

### `static/print_viewer.js`

`parseSTL()` reads binary and ASCII STLs and returns positions, triangle count and
bounds. Like three.js's loader it allows trailing bytes: an exact length fit is binary
whatever the header says, and a longer file is binary only when it does not start with
`solid`. `PrintViewer` builds the scene, maps STL `(x, y, z)` to three.js `(x, z, -y)` so
Z is up like the G-code viewer, centres the footprint, sits the part on a bed plane at
`y = 0` sized from the printer profile, and floats the grid a hair above the bed so it
does not z-fight the part's bottom face. `fitDistance()` frames the part by its own span
with the bed only setting a floor, so a 12 mm part on a 256 mm bed is not a speck.
`resetView()` restores that framing and is bound to the Reset view button. `setTheme()`
repaints the background, bed, part and grid; the grid is rebuilt rather than recoloured,
because a `GridHelper` bakes its colours into vertex colours.

### The two touches to the CNC wizard

- `templates/wizard.html` gains one radio in the mode fieldset:
  `3D Printing — slice a part for the Bambu Lab printer`.
- `static/wizard.js` navigates to
  `/print?source=…&theme=…&return=<current path and query>` when that radio is chosen,
  and does not change the CNC mode state.
- `static/wizard.css` gains two rules for the print page, `.print-value` and
  `#print-canvas`.

Both pages re-assert their own radio at init, because a browser form restore can leave
another radio checked without firing a change event. The CNC page checks the radio for its
current mode; the print page checks the `print` radio. That covers a fresh load, a reload
and a form restore, because each of those runs the page's scripts.

A bfcache Back from `/print` to `/app` does not run them. The browser restores the page as
it was, so the 3D Printing radio can stay checked until the student picks another mode. A
`pageshow` listener would close that gap. On the print page, choosing any CNC radio
navigates to `returnUrl`, and that page reloads with its default mode selected.

## 10. macOS differences

The macOS path is written and untested in this session. Known differences:

- **Quarantine.** `curl` sets no quarantine attribute, but the install script clears
  `com.apple.quarantine` from `OrcaSlicer.app` with `xattr -dr` anyway, in case the image
  was ever opened by Finder, so Gatekeeper does not block the command line.
- **`--datadir`.** Where the macOS build puts its configuration when `XDG_CONFIG_HOME` is
  set has not been confirmed. `--datadir` is passed on every platform and points into the
  job's scratch directory, so it should hold there too.
- **No `result.json`.** The macOS build does not write it, so `warnings` is always empty
  on a Mac. Everything else in the summary comes from the archive and is unaffected.
- **Untested.** That the copied `OrcaSlicer.app` runs headless from the command line on a
  Mac has not been verified.

## 11. Testing

```bash
make test-quick   # everything except the real slice; the tool for iteration
make test         # test-quick plus the Orca integration run; the gate before a change is done
```

`make test-quick` runs `unittest discover -s tests --buffer` and then
`gcode_test.py --quiet`. `make test` runs `test-quick` and then
`tests.orca_integration_test` explicitly. The integration module is excluded by its name,
not by an environment variable: `orca_integration_test.py` does not match the `test_*.py`
discovery pattern. Orca missing is a failure, not a skip: `print_slicer.py` raises at
import, so every test module that imports the app fails without it.

| Test file | Runs Orca | Covers |
|-----------|-----------|--------|
| `tests/test_install_orca.py` | No | `scripts/install-orca.sh` with `uname` and `curl` faked on `PATH`: refusing an unknown platform, the idempotent exit, replacing another architecture's install, the checksum mismatch, and the asset chosen per platform |
| `tests/test_flatten_orca_profiles.py` | No | The flattening algorithm against a tiny fake profile tree: child over parent, self-contained output, a missing profile named in the error, the output format, and the three default sources |
| `tests/test_make_sample_part.py` | No | Twenty triangles, a closed mesh, and the checked-in STL equal to the generator's output |
| `tests/test_print_slicer.py` | No | `subprocess.run` replaced: the command line and flags, the created output directory and the XDG environment, absolute paths, summary parsing from fixtures, warnings from `result.json`, the header read without decompressing the whole entry, missing fields as `None`, timeout, non-zero exit, missing or corrupt archive, `stl_bounds()`, `part_info()`, the import guard, and the profile key checks |
| `tests/test_print_jobs.py` | No | A synchronous stand-in for the executor: ids and state order, the queue cap, failure paths, subscriber wake-up on change and on cadence, `follow()`, expiry, and the documented constants |
| `tests/test_print_routes.py` | No | Through the Flask test client: the page with `source`, `theme` and `return`, a foreign `return`, the `verify=1` redirect, the Onshape frame header, part JSON and STL bytes, 202 and 503 on submit, the record route and its 404, the event stream to its `done`, the 429 on the fourth submit, the worker's move, register and cleanup, and the download MIME type for `.gcode.3mf` |
| `tests/test_print_wizard_page.py` | No | The rendered page carries the header, step bar, radios, print fields and no machine dropdown, the theme and source reach the document, and the CNC Setup offers 3D Printing |
| `tests/test_js_units.py` | No | Runs `node --test` over `tests/js/*.test.js`, which unit-test the pure helpers of `print_wizard.js` and `print_viewer.js` (`formatDuration`, `fitsBed`, `summaryLine`, `printPageUrl`, `statusText`, `tooBigMessage`, `errorForStatus`, `appendNote`, `parseSTL`, `fitDistance`, and the viewer's `load` and `setTheme`). Skipped when node is absent |
| `tests/orca_integration_test.py` | Yes | The wrapper runs, the `VERSION` stamp matches the pinned release, the sample part slices to a valid archive with positive time, filament and layer count, the checked-in profiles still match the installed Orca, and a job runs to `done` with a downloadable zip |

`.github/workflows/integration.yaml` provisions uv and a 3.11 venv, caches `tools/orca`
keyed on the runner image and a hash of `scripts/install-orca.sh`, runs the install
script, then runs `make test`. The cache key includes the image identity because
`hostlibs` holds only what that image lacked at install time.

## 12. Open items and later stages

Open items in stage 1:

- **No thumbnails.** The headless run writes no thumbnail into the archive: Orca's log
  says the OpenGL context is unavailable and it skips thumbnail generation. The printer
  screen and the Bambu apps therefore show no preview image for these files. Whether the
  team's printer otherwise accepts a `.gcode.3mf` without thumbnails is unverified. The
  acceptance step for this stage is printing the sample part from the produced file.
- **Pool sizing.** `MAX_RUNNING` stays at one until peak resident memory and the Railway
  plan's vCPU and memory allotment are measured on production. See
  [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md).
- **macOS install untested.** See section 10.
- **bfcache Back leaves the radio checked.** A Back from `/print` to `/app` that comes
  from the back/forward cache does not re-run the page's scripts, so the 3D Printing radio
  can stay checked until the student picks another mode. A `pageshow` listener on the CNC
  page would close it. See section 9.
- **The 3D view needs WebGL.** The container's headless Chrome has none, so the mesh
  cannot be checked there. A browser without WebGL still shows the summary numbers and a
  note, but no mesh.
- **Vercel is unsupported** from this stage on: the serverless build has no Orca binary
  and a 60-second function limit. `app.py` and `vercel.json` are untouched but no longer
  describe a working deployment.

Later stages, from the spec's scope:

- Exporting the part from Onshape instead of the fixed STL.
- Printer, filament and process profiles chosen from team configuration.
- More than one printer or printer brand.
- Layer-by-layer toolpath preview from the sliced G-code.
- Moving, rotating or duplicating parts on the bed.
- Sending the job to the printer over the network.
