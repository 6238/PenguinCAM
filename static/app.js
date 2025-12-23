
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



toggleSectionShowGcode('setup');
