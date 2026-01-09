// @ts-check

const dropZone = document.getElementById('dropZone');
const fileInput = document.getElementById('fileInput');
const fileInfo = document.getElementById('fileInfo');
const fileName = document.getElementById('fileName');
const fileSize = document.getElementById('fileSize');
const generateBtn = document.getElementById('generateBtn');
const downloadBtn = document.getElementById('downloadBtn');
const driveBtn = document.getElementById('driveBtn');
const driveStatus = document.getElementById('driveStatus');
const loading = document.getElementById('loading');
const results = document.getElementById('results');
const errorAlert = document.getElementById('errorAlert');
const errorMessage = document.getElementById('errorMessage');
const stats = document.getElementById('stats');
const consoleOutput = document.getElementById('consoleOutput');
const materialSelect = document.getElementById('material');
const tubeParams = document.getElementById('tubeParams');
const setupContainer = document.getElementById('dxf-setup-container');
const previewContainer = document.getElementById('canvas-container');
const scrubberContainer = document.getElementById('scrubberContainer');
const previewControls = document.getElementById('previewControls');
const gcodeButtons = document.getElementById('gcodeButtons');
const playbackControl = document.getElementById('playbackControls')

/** Toggle between "setup" and "gcode" modes */
function toggleSectionShowGcode(mode) {
    document.querySelectorAll('.mode-button').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.mode === mode);
    });

    let show = (mode === 'gcode');
    setupContainer.style.display = show ? 'none' : 'block';
    previewContainer.style.display = show ? 'block' : 'none';
    scrubberContainer.style.display = show ? 'flex' : 'none';
    previewControls.style.display = show ? 'flex' : 'none';
    gcodeButtons.style.display = show ? 'flex' : 'none';
    playbackControl.style.display = show ? 'flex' : 'none';

    if (mode === 'setup') {
        dxf_preview.show();

    } else {
        dxf_preview.hide();
    }
}

const dxf_preview = new DXFSetupPreview({
    canvas: '#dxfSetupCanvas',
    inputs: {
        x: '#setupX',
        y: '#posY',
        rotation: '#rot',
        scale: '#scale'
    },
    colors: ["#58A6FF", "#7EE787", "#D29922", "#DB6D28", "#F85149", "#A371F7"]
});

    
const dxfUploader = new FileUploader({
    selectors: {
        dropZone: '#dropZone',
        fileInput: '#fileInput',
        fileList: '#fileList',
        uploadsSection: '#uploadsSection',
        errorDisplay: '#errorMessage'
    },
    onFileChanged: async (id, file, action) => {
        if (action === 'added') {
            const text = await file.text();
            const parser = new window.DxfParser();
            const dxf = parser.parseSync(text);
            dxf_preview.addPart(id, file.name, dxf.entities);
        }
        else if (action === 'removed') {
            dxf_preview.removePart(id);
        } else {
            alert("Unknown file event: " + action);
        }
    },
    onFileClick: (id, file) => {
        dxf_preview.selectPart(id);
    }
});

generateBtn.disabled = false;
generateBtn.textContent = '🚀 Generate G-code';
// Generate G-code
generateBtn.addEventListener('click', async () => {
    const dxfString = dxf_preview.stitchPartsTogether();
    const blob = new Blob([dxfString], { type: 'application/dxf' });
    const file = new File([blob], "nested_layout.dxf", { type: "application/dxf" });

    const formData = new FormData();
    formData.append('file', file);
    const material = document.getElementById('material').value;
    formData.append('material', material);
    formData.append('tool_diameter', document.getElementById('toolDiameter').value);
    formData.append('origin_corner', 'bottom-left'); // Always bottom-left

    // Add material-specific parameters
    if (material === 'aluminum_tube') {
        // Tube-specific parameters
        formData.append('thickness', document.getElementById('thickness').value); // Tube wall thickness
        formData.append('tube_height', document.getElementById('tubeHeight').value);
        formData.append('square_end', document.getElementById('squareEnd').checked ? '1' : '0');
        formData.append('cut_to_length', document.getElementById('cutToLength').checked ? '1' : '0');
    } else {
        // Standard parameters
        formData.append('thickness', document.getElementById('thickness').value);
        formData.append('tabs', document.getElementById('tabs').value);
    }
    formData.append('rotation', 0); // me when i lie

    try {
        const response = await fetch('/process', {
            method: 'POST',
            body: formData
        });

        const data = await response.json();

        if (!response.ok) {
            // Include details if available
            const errorMsg = data.error || 'Unknown error';
            const details = data.details ? `\n\n${data.details}` : '';
            throw new Error(errorMsg + details);
        }

    } catch (error) {
        if (Object.hasOwn(error, "details")) {
            console.error(error.details);
        }
        showError('Generation Failed', error.message);
    } finally {
        // hideLoading();
    }
toggleSectionShowGcode('gcode');

});



toggleSectionShowGcode('setup');
