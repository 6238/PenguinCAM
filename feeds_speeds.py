"""Feeds & speeds calculation core for FRC CNC routers.

This module is intentionally free of any web-framework or PenguinCAM dependency so
that the same model can later be imported directly by ``frc_cam_postprocessor.py``
(the v3 config path) as well as served by the standalone web calculator.

Design principle: **every input is a number you can look up.** Tool numbers come off
the tool's datasheet or product listing, material numbers off a machinability table,
machine numbers off the machine/spindle spec sheet. Nothing is expressed relative to
some other tool or material, and there are no hand-tuned fudge factors standing in
for physics the model declines to represent.

Two consequences of that principle drive the whole model:

1. **Chipload scales with diameter, so the material table stores it as a percentage
   of diameter** (``fz_percent_range``), not as an absolute number for one reference
   tool. A 1/4" cutter takes a bigger bite than a 1/8" cutter in the same material;
   quoting ``fz`` as ~0.75% of diameter captures that directly. When the tool's own
   datasheet lists an absolute ``fz``, that always wins.

2. **Radial engagement (``ae``) is an explicit input**, so radial chip thinning is
   computed from geometry rather than approximated by a "slotting multiplier". At low
   radial engagement the chip is thinner than the programmed feed per tooth, and the
   feed must be compensated *upward* to keep the tool cutting instead of rubbing.

Model (all lengths in inches, feeds in IPM, RPM in rev/min)::

    Vc        = tool.datasheet_sfm  or  material.sfm * coating_factor * substrate_factor
    rpm_ideal = Vc * 12 / (pi * D)
    rpm       = clamp(rpm_ideal, machine.rpm_min, machine.rpm_max)
    Vc_actual = rpm * pi * D / 12                    # what you really get after clamping

    fz_base   = tool.datasheet_fz  or  material.fz_percent * D
    fz_base  *= FULL_SLOT_FZ_DERATE  if the cut is a full-width slot
    rctf      = 1 / (2*sqrt(r*(1-r)))  where r = ae/D, for ae < D/2, else 1.0
    fz_prog   = fz_base * rctf * rigidity_factor
    feed      = min(rpm * flutes * fz_prog, machine.max_feed)
    h_max     = fz_prog / rctf                       # the chip the tool actually takes

    mrr       = ae * ap * feed
    power_hp  = mrr * material.specific_cutting_energy

The spindle power check models a VFD router spindle as **constant torque below its
base speed**: a 3 HP spindle rated at 24,000 RPM delivers roughly 0.75 HP at 6,000
RPM. This is the effect that surprises people cutting steel on a router, where the
surface-speed limit forces you down to a quarter of the spindle's rated output.
"""

import math

# --- Physical / modelling constants ---------------------------------------------

# Radial engagement at or above this fraction of diameter counts as a full-width
# slot: no chip thinning benefit, chips have nowhere to evacuate, and heat stays in
# the cut. Tool vendors publish a separate (lower) fz column for slotting; this is
# that column, expressed as a derate.
FULL_SLOT_AE_RATIO = 0.90
FULL_SLOT_FZ_DERATE = 0.70

# Cap on radial chip thinning compensation. Below ~7% radial engagement the geometric
# factor grows without bound, and blindly following it produces feeds that no router
# can hold and that ignore tool deflection.
MAX_CHIP_THINNING_COMP = 2.0

# Machine rigidity multiplies the programmed chipload: a stiffer machine holds the
# tool on its intended path under cutting load, so it can carry a bigger chip.
# Pick by frame: light = hobby gantry / extruded aluminum, medium = welded-steel
# gantry router (Omio, Avid), heavy = knee mill or VMC.
RIGIDITY_FACTOR = {'light': 0.85, 'medium': 1.00, 'heavy': 1.10}

# Surface speed multipliers for what the tool is made of and coated with. The
# material tables below quote Vc for general-purpose *coated carbide*, so that is the
# 1.00 baseline.
SUBSTRATE_FACTOR = {'carbide': 1.00, 'cobalt': 0.45, 'hss': 0.35}
COATING_FACTOR = {
    'uncoated': 0.75,
    'tin': 0.90,
    'ticn': 1.00,
    'altin_tialn': 1.20,   # AlTiN / TiAlN / SIRON-A: heat-resistant, for steels
    'zrn_dlc': 1.00,       # ZrN / DLC / polished: for aluminum and plastics
}

# Fraction of rated spindle power actually usable, before the constant-torque
# derate below base speed is applied. Covers belt/bearing losses and VFD headroom.
SPINDLE_POWER_UTILISATION = 0.80

# An RPM clamp only matters if it moves surface speed appreciably; a 3% miss is noise.
SFM_CLAMP_WARN_TOLERANCE = 0.10

# Warn when the flute length the tool is buried in exceeds this multiple of
# diameter: deflection grows with the cube of stickout.
DEFLECTION_LD_WARN = 3.0

# ISO material groups, used to match a tool's rating against the workpiece.
ISO_GROUP_NAMES = {
    'P': 'Steel',
    'M': 'Stainless steel',
    'K': 'Cast iron',
    'N': 'Non-ferrous (aluminum, brass, plastics, wood)',
    'S': 'High-temp alloy / titanium',
    'H': 'Hardened steel',
}


MACHINES = {
    'avid_pro2424': {
        'name': 'Avid CNC Pro2424',
        'rpm_min': 6000, 'rpm_max': 24000,
        'max_feed': 400.0, 'max_plunge_feed': 100.0,
        'spindle_power_hp': 3.0, 'power_base_rpm': 24000,
        'rigidity': 'heavy',
    },
    'omio_x8': {
        'name': 'Omio X8-2200',
        'rpm_min': 6000, 'rpm_max': 24000,
        'max_feed': 150.0, 'max_plunge_feed': 60.0,
        'spindle_power_hp': 2.2, 'power_base_rpm': 24000,
        'rigidity': 'medium',
    },
    'generic_light_router': {
        'name': 'Generic light router',
        'rpm_min': 8000, 'rpm_max': 30000,
        'max_feed': 100.0, 'max_plunge_feed': 40.0,
        'spindle_power_hp': 1.25, 'power_base_rpm': 30000,
        'rigidity': 'light',
    },
}


# Machinability data. Sources: Machinery's Handbook cutting-speed tables, tooling
# vendor material charts, and (for the wood/plastic entries, which the handbook does
# not cover) PenguinCAM's own field-proven presets.
#
#   sfm_range          surface speed for general-purpose COATED CARBIDE, [low, high]
#   fz_percent_range   feed per tooth as a PERCENTAGE OF TOOL DIAMETER, [low, high]
#   max_ap_ratio       deepest sane axial cut when slotting, as a multiple of diameter
#   max_ramp_angle     steepest helical/linear ramp the material tolerates, degrees
#   plunge_ratio       straight-plunge feed as a fraction of the XY feed
#   specific_cutting_energy   HP per cubic inch per minute removed
#   max_flutes_soft    above this flute count, chips pack instead of clearing
MATERIALS = {
    'plywood': {
        'name': 'Plywood', 'iso_group': 'N',
        'hardness': 'n/a (wood)',
        'sfm_range': [550, 950],
        'fz_percent_range': [2.0, 5.0],
        'max_ap_ratio': 2.5, 'max_ramp_angle': 15.0, 'plunge_ratio': 0.46,
        'specific_cutting_energy': 0.05, 'max_flutes_soft': 2,
        'notes': 'Wood is chipload-driven, not surface-speed-driven. Run the spindle '
                 'fast and feed hard; too slow a feed burns rather than cuts.',
    },
    'polycarbonate': {
        'name': 'Polycarbonate', 'iso_group': 'N',
        'hardness': 'R118 Rockwell M',
        'sfm_range': [500, 1000],
        'fz_percent_range': [2.0, 4.5],
        'max_ap_ratio': 1.6, 'max_ramp_angle': 15.0, 'plunge_ratio': 0.26,
        'specific_cutting_energy': 0.10, 'max_flutes_soft': 1,
        'notes': 'Melts before it burns. A single flute and a big chip carry heat out '
                 'in the chip; a fine chip welds to the tool.',
    },
    'hdpe': {
        'name': 'HDPE', 'iso_group': 'N',
        'hardness': 'Shore D60-70',
        'sfm_range': [500, 1200],
        'fz_percent_range': [2.5, 5.0],
        'max_ap_ratio': 1.6, 'max_ramp_angle': 15.0, 'plunge_ratio': 0.30,
        'specific_cutting_energy': 0.08, 'max_flutes_soft': 1,
        'notes': 'Gummy. Needs a big chip gap and a sharp, polished flute.',
    },
    'srpp': {
        'name': 'SRPP (polypropylene composite)', 'iso_group': 'N',
        'hardness': 'n/a (composite)',
        'sfm_range': [500, 1000],
        'fz_percent_range': [2.0, 4.5],
        'max_ap_ratio': 1.6, 'max_ramp_angle': 15.0, 'plunge_ratio': 0.28,
        'specific_cutting_energy': 0.10, 'max_flutes_soft': 1,
        'notes': 'Layered composite. Climb mill to avoid lifting and fraying plies.',
    },
    'aluminum_6061': {
        'name': '6061 Aluminum', 'iso_group': 'N',
        'hardness': '95 HB (T6)',
        'sfm_range': [500, 1000],
        'fz_percent_range': [1.5, 3.0],
        'max_ap_ratio': 1.25, 'max_ramp_angle': 5.0, 'plunge_ratio': 0.28,
        'specific_cutting_energy': 0.30, 'max_flutes_soft': 3,
        'notes': 'Wants an uncoated or ZrN/DLC polished flute. AlTiN sticks to '
                 'aluminum. Air blast or mist is close to mandatory.',
    },
    'steel_1018': {
        'name': '1018 Mild Steel', 'iso_group': 'P',
        'hardness': '126 HB',
        'sfm_range': [250, 400],
        'fz_percent_range': [0.6, 1.4],
        'max_ap_ratio': 0.20, 'max_ramp_angle': 2.0, 'plunge_ratio': 0.15,
        'specific_cutting_energy': 1.1, 'max_flutes_soft': 6,
        'notes': 'Gummy for a steel; produces long stringy chips. Keep the feed up so '
                 'the tool cuts rather than burnishes.',
    },
    'steel_1045': {
        'name': '1045 Carbon Steel', 'iso_group': 'P',
        'hardness': '170-210 HB (cold rolled)',
        'sfm_range': [200, 350],
        'fz_percent_range': [0.5, 1.2],
        'max_ap_ratio': 0.15, 'max_ramp_angle': 2.0, 'plunge_ratio': 0.15,
        'specific_cutting_energy': 1.3, 'max_flutes_soft': 6,
        'notes': 'On a router the binding constraint is surface speed: the spindle '
                 'often cannot turn slowly enough for a large tool. Prefer a small '
                 'diameter, a coated cutter, light radial engagement and air blast.',
    },
    'stainless_304': {
        'name': '304 Stainless', 'iso_group': 'M',
        'hardness': '170 HB',
        'sfm_range': [150, 250],
        'fz_percent_range': [0.5, 1.0],
        'max_ap_ratio': 0.10, 'max_ramp_angle': 1.5, 'plunge_ratio': 0.12,
        'specific_cutting_energy': 1.5, 'max_flutes_soft': 6,
        'notes': 'Work-hardens the instant the tool stops cutting. Never dwell, never '
                 'let the chipload drop. Generally past what a router should attempt.',
    },
}


# Tool presets. ``datasheet_*`` fields are the numbers a vendor publishes; when set
# they override the material table. Leave them None to fall back to the material.
def _tool(name, diameter, flutes, loc, substrate='carbide', coating='ticn',
          iso_groups=('N',), datasheet_sfm=None, datasheet_fz=None, source=''):
    return {
        'name': name, 'diameter': diameter, 'flutes': flutes, 'loc': loc,
        'substrate': substrate, 'coating': coating, 'iso_groups': list(iso_groups),
        'datasheet_sfm': datasheet_sfm, 'datasheet_fz': datasheet_fz,
        'source': source,
    }


TOOL_PRESETS = {
    'seco_c5131_4mm': _tool(
        'Seco C5131 4mm 3FL SIRON-A', 0.1575, 3, 0.315,
        coating='altin_tialn', iso_groups=('P', 'M', 'K', 'S'),
        source='MSC 55351241 / Seco 10268797'),
    '4mm_1f': _tool('4mm 1-flute (PenguinCAM default)', 0.157, 1, 0.472,
                    coating='zrn_dlc', iso_groups=('N',)),
    '3mm_1f': _tool('3mm 1-flute', 0.118, 1, 0.354,
                    coating='zrn_dlc', iso_groups=('N',)),
    '125_1f': _tool('1/8" 1-flute', 0.125, 1, 0.375,
                    coating='zrn_dlc', iso_groups=('N',)),
    '250_1f': _tool('1/4" 1-flute', 0.250, 1, 0.750,
                    coating='zrn_dlc', iso_groups=('N',)),
    '250_2f': _tool('1/4" 2-flute', 0.250, 2, 0.750,
                    coating='zrn_dlc', iso_groups=('N',)),
    '250_4f_steel': _tool('1/4" 4-flute AlTiN (steel)', 0.250, 4, 0.750,
                          coating='altin_tialn', iso_groups=('P', 'M', 'K')),
}


# Operations set the default engagement. ``ae_ratio`` is radial depth as a fraction of
# diameter; ``ap_ratio`` is axial depth as a fraction of the material's slotting max
# (adaptive strategies trade radial engagement away to buy axial depth).
OPERATIONS = {
    'slot': {
        'name': 'Slot / full-width cut', 'ae_ratio': 1.00, 'ap_ratio': 1.00,
        'blurb': 'The tool is buried at full diameter. Worst case for heat and chip '
                 'evacuation, so axial depth has to come down.',
    },
    'profile': {
        'name': 'Profile / perimeter cut', 'ae_ratio': 1.00, 'ap_ratio': 1.00,
        'blurb': 'Cutting a part free through full material thickness is a slot: the '
                 'tool is enclosed on both sides.',
    },
    'pocket_adaptive': {
        'name': 'Pocket, adaptive / trochoidal', 'ae_ratio': 0.10, 'ap_ratio': 4.00,
        'blurb': 'Light radial bite, deep axial cut. Spreads wear along the flute and '
                 'keeps cutting forces low, which is what makes hard materials '
                 'possible on a light machine.',
    },
    'pocket_conventional': {
        'name': 'Pocket, conventional offset', 'ae_ratio': 0.40, 'ap_ratio': 1.00,
        'blurb': 'Concentric offset passes at moderate radial engagement. What '
                 'PenguinCAM emits today for non-circular pockets.',
    },
    'finish': {
        'name': 'Finishing pass', 'ae_ratio': 0.05, 'ap_ratio': 4.00,
        'blurb': 'A spring pass at full depth taking a sliver of stock. Chip thinning '
                 'dominates: without feed compensation the tool rubs rather than cuts.',
    },
    'helical_bore': {
        'name': 'Helical bore / ramp into hole', 'ae_ratio': 1.00, 'ap_ratio': 1.00,
        'blurb': 'Spiralling down to open a hole. Radially the tool is fully engaged, '
                 'so the ramp angle, not the axial depth, is the limit.',
    },
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def _mid(pair):
    return (pair[0] + pair[1]) / 2.0


def _resolve(spec, presets, kind):
    """Resolve a machine/material/tool argument to a dict.

    ``spec`` may be a preset key (str) or a dict. A dict may carry a ``preset`` key
    naming a base preset whose values are overlaid with the remaining keys, so the
    UI can start from a preset and tweak a field or two.
    """
    if isinstance(spec, str):
        if spec not in presets:
            raise ValueError(f"Unknown {kind}: {spec!r}. Options: {sorted(presets)}")
        return dict(presets[spec])
    if isinstance(spec, dict):
        base = {}
        if spec.get('preset'):
            base = dict(presets.get(spec['preset'], {}))
        base.update({k: v for k, v in spec.items() if k != 'preset'})
        return base
    raise TypeError(f"{kind} must be a preset key or dict, got {type(spec).__name__}")


def chip_thinning_factor(ae, diameter):
    """Radial chip thinning compensation factor (multiply the feed by this).

    Below half-diameter radial engagement the tool never reaches full chip thickness:
    the maximum chip is ``fz * 2*sqrt(r*(1-r))`` for ``r = ae/D``. To make the tool
    actually take the chip you asked for, the programmed feed must be divided by that
    -- hence a factor >= 1. At and above half diameter there is no thinning.
    """
    if diameter <= 0 or ae <= 0:
        return 1.0
    r = _clamp(ae / diameter, 0.0, 1.0)
    if r >= 0.5:
        return 1.0
    return 1.0 / (2.0 * math.sqrt(r * (1.0 - r)))


def calculate_feeds(machine, material, tool, operation='profile',
                    ae_override=None, ap_override=None, bore_diameter=None):
    """Compute feeds & speeds from datasheet-level inputs.

    Args:
        machine: preset key (e.g. ``'avid_pro2424'``) or a dict of machine fields.
        material: preset key (e.g. ``'steel_1045'``) or a dict of material fields.
        tool: preset key or a dict with at least ``diameter`` and ``flutes``.
        operation: key into ``OPERATIONS``.
        ae_override: radial engagement in inches, overriding the operation default.
        ap_override: axial depth of cut in inches, overriding the operation default.
        bore_diameter: for ``helical_bore``, the finished hole diameter in inches.

    Returns a dict of results, ``warnings``, an ordered ``steps`` list showing where
    each number came from, and the ``formulas`` used -- all JSON-serializable.
    """
    m = _resolve(machine, MACHINES, 'machine')
    mat = _resolve(material, MATERIALS, 'material')
    t = _resolve(tool, TOOL_PRESETS, 'tool')

    if operation not in OPERATIONS:
        raise ValueError(f"Unknown operation: {operation!r}. "
                         f"Options: {sorted(OPERATIONS)}")
    op = OPERATIONS[operation]

    diameter = float(t['diameter'])
    flutes = int(t['flutes'])
    if diameter <= 0 or flutes <= 0:
        raise ValueError("tool diameter and flutes must be positive")

    warnings = []
    steps = []

    def step(label, value, formula, source):
        steps.append({'n': len(steps) + 1, 'label': label, 'value': value,
                      'formula': formula, 'source': source})

    # --- 1. Surface speed --------------------------------------------------------
    substrate = t.get('substrate', 'carbide')
    coating = t.get('coating', 'ticn')
    sub_factor = SUBSTRATE_FACTOR.get(substrate, 1.0)
    coat_factor = COATING_FACTOR.get(coating, 1.0)

    if t.get('datasheet_sfm'):
        sfm_target = float(t['datasheet_sfm'])
        sfm_source = 'Tool datasheet (overrides the material table)'
        sfm_formula = f"Vc = {sfm_target:.0f} SFM, as published for this tool"
    else:
        sfm_base = _mid(mat['sfm_range'])
        sfm_target = sfm_base * sub_factor * coat_factor
        sfm_source = (f"Material table midpoint for {mat['name']}, adjusted for a "
                      f"{substrate} tool with {coating} coating")
        sfm_formula = (f"Vc = {sfm_base:.0f} x {sub_factor:.2f} ({substrate}) "
                       f"x {coat_factor:.2f} ({coating}) = {sfm_target:.0f} SFM")
    step('Target surface speed', f"{sfm_target:.0f} SFM", sfm_formula, sfm_source)

    # --- 2. Spindle RPM ----------------------------------------------------------
    rpm_ideal = sfm_target * 12.0 / (math.pi * diameter)
    rpm = _clamp(rpm_ideal, m['rpm_min'], m['rpm_max'])
    sfm_actual = rpm * math.pi * diameter / 12.0
    step('Spindle RPM', f"{rpm:.0f} RPM",
         f"RPM = Vc x 12 / (pi x D) = {sfm_target:.0f} x 12 / (pi x {diameter:.4f}) "
         f"= {rpm_ideal:.0f}"
         + ("" if abs(rpm - rpm_ideal) < 1 else
            f", clamped to the machine's {m['rpm_min']:.0f}-{m['rpm_max']:.0f} range"),
         "Tool diameter and the target surface speed; machine spec sheet for limits")

    sfm_error = abs(sfm_actual - sfm_target) / sfm_target if sfm_target else 0.0
    if rpm_ideal > m['rpm_max'] + 1 and sfm_error > SFM_CLAMP_WARN_TOLERANCE:
        warnings.append(
            f"Spindle too slow for this surface speed: wanted {rpm_ideal:.0f} RPM but "
            f"the machine tops out at {m['rpm_max']:.0f}, giving {sfm_actual:.0f} SFM "
            f"instead of {sfm_target:.0f}. The tool will cut, just below its best "
            f"speed. A smaller diameter would recover it.")
    elif rpm_ideal < m['rpm_min'] - 1 and sfm_error > SFM_CLAMP_WARN_TOLERANCE:
        warnings.append(
            f"Spindle cannot turn slowly enough: wanted {rpm_ideal:.0f} RPM but the "
            f"minimum is {m['rpm_min']:.0f}, forcing {sfm_actual:.0f} SFM against a "
            f"target of {sfm_target:.0f}. The tool will run hot and wear fast. Use a "
            f"SMALLER diameter to bring surface speed back down.")

    # --- 3. Engagement: ae (radial) and ap (axial) -------------------------------
    if ae_override is not None:
        ae = float(ae_override)
        ae_source = 'Entered directly'
    else:
        ae = op['ae_ratio'] * diameter
        ae_source = f"{op['name']} default of {op['ae_ratio']:.2f} x diameter"
    ae = _clamp(ae, 1e-6, diameter)

    ap_ceiling = mat['max_ap_ratio'] * diameter
    if ap_override is not None:
        ap = float(ap_override)
        ap_source = 'Entered directly'
    else:
        ap = op['ap_ratio'] * ap_ceiling
        ap_source = (f"{op['name']} default: {op['ap_ratio']:.2f} x the material's "
                     f"{mat['max_ap_ratio']:.2f} x D slotting limit")

    loc = float(t.get('loc') or 0) or None
    if loc and ap > loc:
        warnings.append(
            f"Axial depth {ap:.4f} in exceeds the tool's {loc:.3f} in length of cut. "
            f"Capped to {loc:.3f} in. A deeper cut needs a longer tool.")
        ap = loc
        ap_source += ', capped by the tool length of cut'

    step('Radial engagement (ae)', f"{ae:.4f} in ({ae / diameter * 100:.0f}% of D)",
         f"ae = {ae / diameter:.3f} x D", ae_source)
    step('Axial depth (ap)', f"{ap:.4f} in ({ap / diameter:.2f} x D)",
         f"ap = {ap / diameter:.3f} x D", ap_source)

    # --- 4. Feed per tooth -------------------------------------------------------
    if t.get('datasheet_fz'):
        fz_base = float(t['datasheet_fz'])
        fz_source = 'Tool datasheet (overrides the material table)'
        fz_formula = f"fz = {fz_base:.5f} in/tooth, as published for this tool"
    else:
        fz_percent = _mid(mat['fz_percent_range'])
        fz_base = fz_percent / 100.0 * diameter
        fz_source = (f"Material table: {mat['name']} takes "
                     f"{mat['fz_percent_range'][0]:.1f}-{mat['fz_percent_range'][1]:.1f}% "
                     f"of tool diameter per tooth")
        fz_formula = (f"fz = {fz_percent:.2f}% x {diameter:.4f} = {fz_base:.5f} in/tooth")
    step('Base feed per tooth', f"{fz_base:.5f} in/tooth", fz_formula, fz_source)

    is_full_slot = (ae / diameter) >= FULL_SLOT_AE_RATIO
    if is_full_slot:
        fz_base *= FULL_SLOT_FZ_DERATE
        step('Full-slot derate', f"{fz_base:.5f} in/tooth",
             f"fz = fz x {FULL_SLOT_FZ_DERATE:.2f}",
             'The tool is enclosed at full width: chips cannot clear and heat stays '
             'in the cut. Vendors publish a separate lower fz for slotting.')

    # --- 5. Radial chip thinning -------------------------------------------------
    rctf_raw = chip_thinning_factor(ae, diameter)
    rctf = min(rctf_raw, MAX_CHIP_THINNING_COMP)
    if rctf_raw > MAX_CHIP_THINNING_COMP:
        warnings.append(
            f"Chip thinning compensation capped at {MAX_CHIP_THINNING_COMP:.1f}x "
            f"(geometry alone asks for {rctf_raw:.1f}x). At {ae / diameter * 100:.0f}% "
            f"radial engagement the correction outruns what the machine and the tool's "
            f"stiffness can safely absorb.")
    if rctf > 1.0:
        step('Chip thinning compensation', f"{rctf:.2f}x",
             f"rctf = 1 / (2 x sqrt(r x (1-r))), r = ae/D = {ae / diameter:.3f}",
             'Geometry: below half-diameter engagement the tool never reaches full '
             'chip thickness, so the feed is raised to compensate. Skipping this is '
             'the most common cause of rubbing and premature tool death.')

    rigidity = m.get('rigidity', 'medium')
    rigidity_factor = RIGIDITY_FACTOR.get(rigidity, 1.0)
    fz_prog = fz_base * rctf * rigidity_factor
    step('Programmed feed per tooth', f"{fz_prog:.5f} in/tooth",
         f"fz_prog = {fz_base:.5f} x {rctf:.2f} (thinning) x {rigidity_factor:.2f} "
         f"({rigidity} frame)",
         'Machine rigidity is a judgement call: light = hobby gantry, medium = '
         'welded-steel router, heavy = knee mill or VMC')

    # --- 6. Feed rate ------------------------------------------------------------
    feed_raw = rpm * flutes * fz_prog
    feed = min(feed_raw, m['max_feed'])
    if feed_raw > m['max_feed']:
        warnings.append(
            f"Feed clamped by the machine: wanted {feed_raw:.1f} IPM, maximum is "
            f"{m['max_feed']:.0f} IPM. The tool will take a thinner chip than intended; "
            f"drop the RPM proportionally to restore it.")
    step('Feed rate', f"{feed:.1f} IPM",
         f"feed = RPM x flutes x fz_prog = {rpm:.0f} x {flutes} x {fz_prog:.5f} "
         f"= {feed_raw:.1f} IPM"
         + ("" if feed_raw <= m['max_feed'] else
            f", clamped to the machine's {m['max_feed']:.0f} IPM"),
         'Spindle RPM, the tool flute count, and the programmed chipload')

    # What the tool actually experiences after every clamp above.
    fz_actual = feed / (rpm * flutes)
    h_max = fz_actual / rctf if rctf else fz_actual

    # --- 7. Ramp and plunge ------------------------------------------------------
    ramp_angle = mat['max_ramp_angle']
    ramp_feed = min(feed, m['max_feed'])
    ramp_z_feed = min(ramp_feed * math.sin(math.radians(ramp_angle)),
                      m['max_plunge_feed'])
    plunge_feed = min(feed * mat['plunge_ratio'], m['max_plunge_feed'])

    helix_pitch = None
    if operation == 'helical_bore' and bore_diameter:
        helical_path_dia = float(bore_diameter) - diameter
        if helical_path_dia > 0:
            helix_pitch = (math.pi * helical_path_dia
                           * math.tan(math.radians(ramp_angle)))

    # --- 8. Material removal rate and spindle power ------------------------------
    mrr = ae * ap * feed
    power_required = mrr * mat['specific_cutting_energy']

    power_available = None
    rated_hp = m.get('spindle_power_hp')
    if rated_hp:
        base_rpm = m.get('power_base_rpm') or m['rpm_max']
        # VFD router spindles hold constant torque below base speed, so available
        # power falls off linearly with RPM. This is why steel hurts on a router.
        power_available = (rated_hp * SPINDLE_POWER_UTILISATION
                           * min(1.0, rpm / base_rpm))
        if power_required > power_available:
            warnings.append(
                f"Spindle power: this cut needs about {power_required:.2f} HP but only "
                f"~{power_available:.2f} HP is available at {rpm:.0f} RPM (a "
                f"{rated_hp:.1f} HP spindle makes rated power near {base_rpm:.0f} RPM "
                f"and less in proportion below it). Reduce depth of cut or feed.")

    # --- 9. Remaining sanity checks ----------------------------------------------
    tool_groups = t.get('iso_groups') or []
    mat_group = mat.get('iso_group')
    if tool_groups and mat_group and mat_group not in tool_groups:
        warnings.append(
            f"Tool is not rated for this material: it lists ISO "
            f"{', '.join(tool_groups)} and {mat['name']} is ISO {mat_group} "
            f"({ISO_GROUP_NAMES.get(mat_group, '?')}). Geometry and coating are "
            f"matched to the material class, so this is more than a speed question.")

    if flutes > mat['max_flutes_soft']:
        warnings.append(
            f"{flutes}-flute tool in {mat['name']}: soft or gummy materials evacuate "
            f"chips poorly with high flute counts, and the tool may pack and rub. A "
            f"1- or 2-flute cutter clears chips far better here.")

    if ap > 0 and ap / diameter > DEFLECTION_LD_WARN:
        warnings.append(
            f"Engaged flute length is {ap / diameter:.1f}x diameter. Deflection grows "
            f"with the cube of stickout, so expect taper and chatter. Acceptable for "
            f"adaptive cuts at low ae; not for a full-width slot.")

    if mat_group in ('P', 'M') and coating == 'uncoated':
        warnings.append(
            "Uncoated carbide in steel or stainless wears very quickly. An AlTiN or "
            "TiAlN coated cutter lasts several times longer for a few dollars more.")

    if mat_group == 'N' and coating == 'altin_tialn' and 'alumin' in mat['name'].lower():
        warnings.append(
            "AlTiN/TiAlN coating in aluminum: aluminum galls onto this coating. An "
            "uncoated polished flute or a ZrN/DLC coating cuts aluminum far better.")

    explanation = _build_explanation(
        m, mat, t, op, diameter, flutes, rpm, sfm_target, sfm_actual,
        ae, ap, fz_prog, fz_actual, h_max, rctf, feed, is_full_slot)

    formulas = [
        "RPM      = Vc x 12 / (pi x D)                 surface speed -> spindle speed",
        "rctf     = 1 / (2 x sqrt(r x (1-r)))          radial chip thinning, r = ae/D",
        "fz_prog  = fz_base x rctf x rigidity          what you program per tooth",
        "feed     = RPM x flutes x fz_prog             inches per minute",
        "h_max    = fz_prog x 2 x sqrt(r x (1-r))      the chip the tool really takes",
        "MRR      = ae x ap x feed                     cubic inches per minute",
        "power_hp = MRR x specific_cutting_energy      load on the spindle",
    ]
    if helix_pitch is not None:
        formulas.append(
            "pitch    = pi x (D_bore - D_tool) x tan(ramp)  helical drop per revolution")

    return {
        'rpm': round(rpm),
        'sfm_target': round(sfm_target),
        'sfm_actual': round(sfm_actual),
        'feed': round(feed, 1),
        'ramp_feed': round(ramp_feed, 1),
        'ramp_angle': round(ramp_angle, 1),
        'ramp_z_feed': round(ramp_z_feed, 2),
        'plunge_feed': round(plunge_feed, 1),
        'helix_pitch': round(helix_pitch, 4) if helix_pitch is not None else None,
        'ae': round(ae, 4),
        'ae_ratio': round(ae / diameter, 4),
        'ap': round(ap, 4),
        'ap_ratio': round(ap / diameter, 4),
        'fz_programmed': round(fz_prog, 5),
        'fz_actual': round(fz_actual, 5),
        'chip_thickness': round(h_max, 5),
        'chip_thinning_factor': round(rctf, 3),
        'mrr': round(mrr, 4),
        'power_required': round(power_required, 3),
        'power_available': round(power_available, 3) if power_available else None,
        'feed_clamped': feed_raw > m['max_feed'],
        'rpm_clamped': abs(rpm - rpm_ideal) > 1,
        'is_full_slot': is_full_slot,
        'operation': operation,
        'operation_name': op['name'],
        'operation_blurb': op['blurb'],
        'material_notes': mat.get('notes', ''),
        'warnings': warnings,
        'steps': steps,
        'explanation': explanation,
        'formulas': formulas,
    }


def _build_explanation(m, mat, t, op, diameter, flutes, rpm, sfm_target, sfm_actual,
                       ae, ap, fz_prog, fz_actual, h_max, rctf, feed, is_full_slot):
    dia_mm = diameter * 25.4
    parts = [
        f"A {dia_mm:.2f}mm ({diameter:.4f}\") {flutes}-flute "
        f"{t.get('substrate', 'carbide')} cutter in {mat['name']} on the "
        f"{m.get('name', 'machine')}, running a {op['name'].lower()}."
    ]
    if abs(sfm_actual - sfm_target) > 5:
        parts.append(
            f"The target surface speed of {sfm_target:.0f} SFM implies "
            f"{sfm_target * 12 / (math.pi * diameter):.0f} RPM, which is outside the "
            f"spindle's range, so it runs at {rpm:.0f} RPM for an actual "
            f"{sfm_actual:.0f} SFM.")
    else:
        parts.append(
            f"{sfm_target:.0f} SFM at {diameter:.4f}\" diameter calls for "
            f"{rpm:.0f} RPM.")

    if is_full_slot:
        parts.append(
            f"At full width the tool is enclosed, so there is no chip thinning to "
            f"exploit and the feed per tooth is derated for heat.")
    elif rctf > 1.0:
        parts.append(
            f"Radial engagement is only {ae / diameter * 100:.0f}% of diameter, so the "
            f"chip comes out {1 / rctf:.0%} as thick as the feed per tooth would "
            f"suggest. The feed is compensated {rctf:.2f}x to put the chip back where "
            f"it belongs, at {h_max:.5f} in.")

    parts.append(
        f"Feed = {rpm:.0f} RPM x {flutes} flutes x {fz_prog:.5f} in/tooth = "
        f"{feed:.1f} IPM, removing {ae:.4f}\" radially by {ap:.4f}\" axially.")
    return " ".join(parts)


if __name__ == '__main__':
    import json
    demo = calculate_feeds('avid_pro2424', 'steel_1045',
                           TOOL_PRESETS['seco_c5131_4mm'], 'pocket_adaptive')
    print(json.dumps(demo, indent=2))
