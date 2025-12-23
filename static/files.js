function FileUploader(config) {
    this.dropZone = document.querySelector(config.selectors.dropZone);
    this.fileInput = document.querySelector(config.selectors.fileInput);
    this.fileListContainer = document.querySelector(config.selectors.fileList);
    this.uploadsSection = document.querySelector(config.selectors.uploadsSection);
    
    this.acceptedFormat = config.acceptedFormat || '.dxf';
    
    // Updated callbacks to accept UUIDs
    this.onFileChanged = config.onFileChanged || function(id, file, action){}; 
    this.onFileClick = config.onFileClick || function(id, file){}; 

    this.files = []; // Will store objects: { id: '...', file: File }
    this.initEventListeners();
}

FileUploader.prototype.initEventListeners = function() {
    var self = this;

    this.dropZone.addEventListener('click', function() {
        self.fileInput.click();
    });

    this.dropZone.addEventListener('dragover', function(e) {
        e.preventDefault();
        self.dropZone.classList.add('dragover');
    });

    this.dropZone.addEventListener('dragleave', function() {
        self.dropZone.classList.remove('dragover');
    });

    this.dropZone.addEventListener('drop', function(e) {
        e.preventDefault();
        self.dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length > 0) {
            self.processFiles(e.dataTransfer.files);
        }
    });

    this.fileInput.addEventListener('change', function(e) {
        if (e.target.files.length > 0) {
            self.processFiles(e.target.files);
            self.fileInput.value = ''; 
        }
    });
};

// Helper to generate UUID
FileUploader.prototype.generateUUID = function() {
    if (typeof crypto !== 'undefined' && crypto.randomUUID) {
        return crypto.randomUUID();
    }
    // Fallback for older environments
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
        var r = Math.random() * 16 | 0, v = c == 'x' ? r : (r & 0x3 | 0x8);
        return v.toString(16);
    });
};

FileUploader.prototype.processFiles = function(fileList) {
    console.log("Processing files:", fileList);
    Array.from(fileList).forEach(file => {
        this.addFile(file);
    });
};

FileUploader.prototype.addFile = function(file) {
    const id = this.generateUUID();
    
    // Store as tuple/object structure
    this.files.push({
        id: id,
        file: file
    });

    this.renderFileList();
    // Pass ID to the callback
    this.onFileChanged(id, file, 'added');
};

FileUploader.prototype.removeFile = function(id) {
    // Find index by UUID instead of relying on passed index
    const index = this.files.findIndex(item => item.id === id);
    
    if (index > -1) {
        const removedItem = this.files[index];
        this.files.splice(index, 1);
        this.renderFileList();
        
        // Pass ID to the callback
        this.onFileChanged(removedItem.id, removedItem.file, 'removed');
    }
};

FileUploader.prototype.renderFileList = function() {
    this.fileListContainer.innerHTML = '';
    
    if (this.files.length > 0) {
        this.uploadsSection.style.display = 'block';
    } else {
        this.uploadsSection.style.display = 'none';
    }

    this.files.forEach((item) => {
        // Destructure the storage object
        const { id, file } = item;

        const div = document.createElement('div');
        div.className = 'file-item';
        
        // Pass ID and File to click handler
        div.addEventListener('click', () => {
            this.onFileClick(id, file);
        });

        const leftSide = document.createElement('div');
        leftSide.className = 'file-item-left';
        
        leftSide.innerHTML = `
            <div class="file-icon-small">📄</div>
            <div class="file-info-group">
                <span class="file-name">${file.name}</span>
                <span class="file-size">${this.formatFileSize(file.size)}</span>
            </div>
        `;

        const removeBtn = document.createElement('button');
        removeBtn.className = 'remove-file-btn';
        removeBtn.innerHTML = '×';
        removeBtn.title = 'Remove file';
        
        removeBtn.addEventListener('click', (e) => {
            e.stopPropagation(); 
            // Call removeFile with the UUID
            this.removeFile(id);
        });

        div.appendChild(leftSide);
        div.appendChild(removeBtn);
        this.fileListContainer.appendChild(div);
    });
};

FileUploader.prototype.formatFileSize = function(bytes) {
    if (bytes === 0) return '0 Bytes';
    var k = 1024;
    var sizes = ['Bytes', 'KB', 'MB'];
    var i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
};