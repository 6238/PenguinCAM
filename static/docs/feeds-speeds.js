// FRC Router Feeds & Speeds Explainer - talks to the shared Python calc core.
//
// The full preset dict for each of machine/material/tool is kept in state and the
// editable inputs are overlaid onto it, so the backend always receives a complete
// dict including fields the UI does not expose.
'use strict';

// [input id, path into the preset dict, coercion]. A path may index into an array,
// e.g. the material's sfm_range is edited as two separate low/high inputs.
const MACHINE_FIELDS = [
    ['machine-rpm_min', ['rpm_min'], 'number'],
    ['machine-rpm_max', ['rpm_max'], 'number'],
    ['machine-max_feed', ['max_feed'], 'number'],
    ['machine-max_plunge_feed', ['max_plunge_feed'], 'number'],
    ['machine-spindle_power_hp', ['spindle_power_hp'], 'number'],
    ['machine-power_base_rpm', ['power_base_rpm'], 'number'],
    ['machine-rigidity', ['rigidity'], 'string'],
];

const MATERIAL_FIELDS = [
    ['material-sfm_lo', ['sfm_range', 0], 'number'],
    ['material-sfm_hi', ['sfm_range', 1], 'number'],
    ['material-fz_lo', ['fz_percent_range', 0], 'number'],
    ['material-fz_hi', ['fz_percent_range', 1], 'number'],
    ['material-max_ap_ratio', ['max_ap_ratio'], 'number'],
    ['material-max_ramp_angle', ['max_ramp_angle'], 'number'],
];

const TOOL_FIELDS = [
    ['tool-diameter', ['diameter'], 'number'],
    ['tool-flutes', ['flutes'], 'number'],
    ['tool-loc', ['loc'], 'number'],
    ['tool-substrate', ['substrate'], 'string'],
    ['tool-coating', ['coating'], 'string'],
    ['tool-iso_groups', ['iso_groups'], 'list'],
    ['tool-datasheet_sfm', ['datasheet_sfm'], 'optional-number'],
    ['tool-datasheet_fz', ['datasheet_fz'], 'optional-number'],
];

const OVERRIDE_INPUTS = ['ae_override', 'ap_override', 'bore_diameter'];

const state = {
    presets: null,
    machine: null,
    material: null,
    tool: null,
};

const $ = (id) => document.getElementById(id);

// --- Path helpers so array-valued preset fields edit like any other ---------------
function readPath(obj, path) {
    return path.reduce((acc, key) => (acc == null ? acc : acc[key]), obj);
}

function writePath(obj, path, value) {
    let node = obj;
    for (let i = 0; i < path.length - 1; i += 1) {
        if (node[path[i]] == null) node[path[i]] = typeof path[i + 1] === 'number' ? [] : {};
        node = node[path[i]];
    }
    node[path[path.length - 1]] = value;
}

function toInput(value, type) {
    if (type === 'list') return Array.isArray(value) ? value.join(', ') : (value || '');
    if (value === null || value === undefined) return '';
    return value;
}

function fromInput(raw, type) {
    if (type === 'string') return raw;
    if (type === 'list') {
        return raw.split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
    }
    if (type === 'optional-number') {
        return raw === '' ? null : parseFloat(raw);
    }
    const n = parseFloat(raw);
    return Number.isNaN(n) ? 0 : n;
}

async function init() {
    const resp = await fetch('/api/feeds-speeds/presets');
    state.presets = await resp.json();

    populateSelect('machine-preset', state.presets.machines);
    populateSelect('material-preset', state.presets.materials);
    populateSelect('tool-preset', state.presets.tools);
    populateSelect('operation', state.presets.operations);

    loadPreset('machine', 'avid_pro2424');
    loadPreset('material', 'steel_1045');
    loadPreset('tool', 'seco_c5131_4mm');
    $('operation').value = 'pocket_adaptive';

    $('machine-preset').addEventListener('change', (e) => {
        loadPreset('machine', e.target.value); recalc();
    });
    $('material-preset').addEventListener('change', (e) => {
        loadPreset('material', e.target.value); recalc();
    });
    $('tool-preset').addEventListener('change', (e) => {
        loadPreset('tool', e.target.value); recalc();
    });

    bindFields('machine', MACHINE_FIELDS);
    bindFields('material', MATERIAL_FIELDS);
    bindFields('tool', TOOL_FIELDS);

    $('operation').addEventListener('change', () => { syncOperation(); recalc(); });
    OVERRIDE_INPUTS.forEach((id) => $(id).addEventListener('input', recalc));

    syncOperation();
    recalc();
}

function bindFields(kind, fields) {
    fields.forEach(([id]) => {
        $(id).addEventListener('input', () => { syncGroup(kind, fields); recalc(); });
        $(id).addEventListener('change', () => { syncGroup(kind, fields); recalc(); });
    });
}

function populateSelect(id, items) {
    const sel = $(id);
    sel.innerHTML = '';
    Object.entries(items).forEach(([key, val]) => {
        const opt = document.createElement('option');
        opt.value = key;
        opt.textContent = val.name || key;
        sel.appendChild(opt);
    });
}

const FIELD_GROUPS = {
    machine: MACHINE_FIELDS,
    material: MATERIAL_FIELDS,
    tool: TOOL_FIELDS,
};

function loadPreset(kind, key) {
    // Deep copy so editing an input never mutates the preset we fetched.
    state[kind] = JSON.parse(JSON.stringify(state.presets[kind + 's'][key]));
    $(kind + '-preset').value = key;
    FIELD_GROUPS[kind].forEach(([id, path, type]) => {
        $(id).value = toInput(readPath(state[kind], path), type);
    });
    if (kind === 'material') renderMaterialMeta();
}

function syncGroup(kind, fields) {
    fields.forEach(([id, path, type]) => {
        writePath(state[kind], path, fromInput($(id).value, type));
    });
}

function renderMaterialMeta() {
    const mat = state.material;
    const group = mat.iso_group ? `ISO ${mat.iso_group}` : '';
    const hardness = mat.hardness ? ` &middot; ${mat.hardness}` : '';
    $('material-meta').innerHTML = group + hardness;
}

function syncOperation() {
    const op = state.presets.operations[$('operation').value];
    $('operation-blurb').textContent = op ? op.blurb : '';
    $('bore-row').hidden = $('operation').value !== 'helical_bore';
}

let recalcTimer = null;
function recalc() {
    clearTimeout(recalcTimer);
    recalcTimer = setTimeout(doRecalc, 150);
}

function optionalNumber(id) {
    const raw = $(id).value;
    return raw === '' ? null : parseFloat(raw);
}

async function doRecalc() {
    const payload = {
        machine: state.machine,
        material: state.material,
        tool: state.tool,
        operation: $('operation').value,
        ae_override: optionalNumber('ae_override'),
        ap_override: optionalNumber('ap_override'),
        bore_diameter: optionalNumber('bore_diameter'),
    };

    let result;
    try {
        const resp = await fetch('/api/feeds-speeds', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        result = await resp.json();
    } catch (err) {
        result = { error: String(err) };
    }

    if (result.error) {
        $('explanation').textContent = 'Error: ' + result.error;
        return;
    }
    render(result);
}

function render(r) {
    $('r-rpm').textContent = r.rpm;
    $('r-sfm').textContent = r.sfm_actual + ' SFM';
    $('r-feed').textContent = r.feed;
    $('r-ae').textContent = r.ae.toFixed(4);
    $('r-ae-pct').textContent = Math.round(r.ae_ratio * 100) + '% of D';
    $('r-ap').textContent = r.ap.toFixed(4);
    $('r-ap-pct').textContent = r.ap_ratio.toFixed(2) + ' x D';
    $('r-fz').textContent = r.fz_programmed.toFixed(5);
    $('r-chip').textContent = r.chip_thickness.toFixed(5);
    $('r-thinning').textContent = r.chip_thinning_factor > 1
        ? 'in, after ' + r.chip_thinning_factor.toFixed(2) + 'x comp'
        : 'in/tooth';
    $('r-ramp').textContent = r.ramp_feed;
    $('r-ramp-unit').textContent = 'IPM at ' + r.ramp_angle + ' deg max ('
        + r.ramp_z_feed + ' IPM in Z)';
    $('r-plunge').textContent = r.plunge_feed;
    $('r-mrr').textContent = r.mrr.toFixed(4);
    $('r-power').textContent = r.power_required.toFixed(2) + ' HP';
    $('r-power-avail').textContent = r.power_available
        ? 'of ~' + r.power_available.toFixed(2) + ' HP available'
        : '';

    const pitchCard = $('r-pitch-card');
    pitchCard.hidden = r.helix_pitch === null;
    if (r.helix_pitch !== null) $('r-pitch').textContent = r.helix_pitch.toFixed(4);

    const warns = $('warnings');
    warns.innerHTML = '';
    r.warnings.forEach((w) => {
        const div = document.createElement('div');
        div.className = 'warning';
        div.textContent = w;
        warns.appendChild(div);
    });

    $('material-notes').textContent = r.material_notes || '';
    $('explanation').textContent = r.explanation;

    const steps = $('steps');
    steps.innerHTML = '';
    r.steps.forEach((s) => {
        const li = document.createElement('li');
        li.className = 'calc-step';

        const head = document.createElement('div');
        head.className = 'calc-step-head';
        const label = document.createElement('span');
        label.className = 'calc-step-label';
        label.textContent = s.label;
        const value = document.createElement('span');
        value.className = 'calc-step-value';
        value.textContent = s.value;
        head.append(label, value);

        const formula = document.createElement('code');
        formula.className = 'calc-step-formula';
        formula.textContent = s.formula;

        const source = document.createElement('div');
        source.className = 'calc-step-source';
        source.textContent = s.source;

        li.append(head, formula, source);
        steps.appendChild(li);
    });

    $('formulas').textContent = r.formulas.join('\n');
}

init();
