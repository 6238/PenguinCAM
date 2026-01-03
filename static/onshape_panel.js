(function() {
    'use strict';

    // State
    let selectedFaceId = null;
    let selectedPartId = null;

    // DOM elements
    const instruction = document.getElementById('instruction');
    const faceInfo = document.getElementById('faceInfo');
    const faceIdDisplay = document.getElementById('faceId');
    const buttonGroup = document.getElementById('buttonGroup');
    const sendBtn = document.getElementById('sendToPenguinCAM');
    const driveBtn = document.getElementById('saveToDrive');

    // Onshape context from template
    const context = window.ONSHAPE_CONTEXT;

    /**
     * Initialize the extension
     * Send applicationInit message to Onshape
     */
    function initialize() {
        console.log('PenguinCAM panel initializing...', context);

        // Send initialization message to Onshape
        const initMessage = {
            messageName: 'applicationInit',
            documentId: context.documentId,
            workspaceId: context.workspaceId,
            elementId: context.elementId
        };

        window.parent.postMessage(initMessage, '*');
        console.log('Sent applicationInit:', initMessage);

        // Listen for messages from Onshape
        window.addEventListener('message', handleMessage);

        // Set up button handlers
        sendBtn.addEventListener('click', handleSendToPenguinCAM);
        driveBtn.addEventListener('click', handleSaveToDrive);
    }

    /**
     * Handle incoming messages from Onshape parent window
     */
    function handleMessage(event) {
        // Validate origin for security
        if (!event.origin.includes('onshape.com')) {
            console.warn('Message from invalid origin:', event.origin);
            return;
        }

        const data = event.data;
        console.log('Received message:', data);

        if (data.messageName === 'SELECTION') {
            handleSelection(data);
        }
    }

    /**
     * Handle selection change from Onshape
     */
    function handleSelection(data) {
        const selections = data.selections || [];
        console.log('Selection changed:', selections);

        // Look for FACE selection
        const faceSelection = selections.find(s =>
            s.entityType === 'FACE' && s.selectionType === 'ENTITY'
        );

        if (faceSelection) {
            // Face selected
            selectedFaceId = faceSelection.selectionId;
            selectedPartId = faceSelection.partId || null;

            // Update UI
            instruction.style.display = 'none';
            faceInfo.style.display = 'block';
            buttonGroup.style.display = 'flex';
            faceIdDisplay.textContent = selectedFaceId;

            // Enable buttons
            sendBtn.disabled = false;
            driveBtn.disabled = false;

            console.log('Face selected:', selectedFaceId, 'Part:', selectedPartId);
        } else {
            // No face selected - reset UI
            selectedFaceId = null;
            selectedPartId = null;

            instruction.style.display = 'block';
            faceInfo.style.display = 'none';
            buttonGroup.style.display = 'none';

            sendBtn.disabled = true;
            driveBtn.disabled = true;

            console.log('No face selected');
        }
    }

    /**
     * Build URL with Onshape context parameters
     */
    function buildUrl(endpoint) {
        const params = new URLSearchParams({
            documentId: context.documentId,
            workspaceId: context.workspaceId,
            elementId: context.elementId,
            server: context.server
        });

        // Add face ID if selected
        if (selectedFaceId) {
            params.append('faceId', selectedFaceId);
        }

        // Add part ID if available
        if (selectedPartId) {
            params.append('partId', selectedPartId);
        }

        return `${context.baseUrl}${endpoint}?${params.toString()}`;
    }

    /**
     * Handle "Send to PenguinCAM" button
     * Opens full PenguinCAM interface in new window
     */
    function handleSendToPenguinCAM() {
        const url = buildUrl('/onshape/import');
        console.log('Opening PenguinCAM:', url);

        // Open in new tab (without window features to make it a tab, not popup)
        window.open(url, '_blank');
    }

    /**
     * Handle "Save DXF to Drive" button
     * Directly saves to Drive without opening full UI
     */
    function handleSaveToDrive() {
        const url = buildUrl('/onshape/save-dxf');
        console.log('Saving to Drive:', url);

        // Show loading state
        driveBtn.disabled = true;
        driveBtn.textContent = 'Saving...';

        // Make request to save endpoint
        fetch(url, {
            method: 'GET',
            credentials: 'include'  // Include cookies for auth
        })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                // Success!
                driveBtn.textContent = 'Saved!';
                driveBtn.style.background = '#4CAF50';

                // Show success message
                alert(`Success! DXF saved to Google Drive:\n${data.filename}`);

                // Reset after delay
                setTimeout(() => {
                    driveBtn.disabled = false;
                    driveBtn.textContent = 'Save DXF to Drive';
                    driveBtn.style.background = '';
                }, 2000);
            } else {
                throw new Error(data.error || 'Save failed');
            }
        })
        .catch(error => {
            console.error('Save error:', error);
            driveBtn.textContent = 'Failed';
            driveBtn.style.background = '#f44336';

            // Show error
            alert(`Error saving to Drive:\n${error.message}`);

            // Reset
            setTimeout(() => {
                driveBtn.disabled = false;
                driveBtn.textContent = 'Save DXF to Drive';
                driveBtn.style.background = '';
            }, 2000);
        });
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
})();
