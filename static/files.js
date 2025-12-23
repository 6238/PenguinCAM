
// File upload handling
dropZone.addEventListener('click', () => fileInput.click());

dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
});

dropZone.addEventListener('dragleave', () => {
    dropZone.classList.remove('dragover');
});

dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    const files = e.dataTransfer.files;
    if (files.length > 0) {
        handleFile(files[0]);
    }
});

fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) {
        handleFile(e.target.files[0]);
    }
});

function handleFile(file) {
    if (!file.name.toLowerCase().endsWith('.dxf')) {
        showError('Invalid file type', 'Please upload a DXF file.');
        return;
    }

    uploadedFile = file;
    fileName.textContent = file.name;
    fileSize.textContent = formatFileSize(file.size);
    fileInfo.style.display = 'flex';
    generateBtn.disabled = false;
    generateBtn.textContent = '🚀 Generate Program';
    hideError();
    hideResults();
    
    // Read DXF file for setup mode
    const reader = new FileReader();
    reader.onload = (e) => {
        parseDxfForSetup(e.target.result);
    };
    reader.readAsText(file);
}


downloadBtn.addEventListener('click', () => {
    if (!outputFilename) return;
    window.location.href = `/download/${outputFilename}`;
});