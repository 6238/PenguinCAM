(function() {
    'use strict';

    // State
    let selectedFaceId = null;
    let selectedPartId = null;
    let currentSelection = null;  // Full selection object for highlighting
    let selectionRequestCounter = 0;
    let isWaitingForSelection = false;

    // DOM elements
    const instruction = document.getElementById('instruction');
    const buttonGroup = document.getElementById('buttonGroup');
    const sendBtn = document.getElementById('sendToBionicsCAM');
    const selectAnotherBtn = document.getElementById('selectAnotherFace');
    const multilayerCheckbox = document.getElementById('multilayerMode');
    const mode2DLabel = document.getElementById('mode2DLabel');
    const mode25DLabel = document.getElementById('mode25DLabel');
    const modeHint = document.getElementById('modeHint');

    // Onshape context from template
    const context = window.ONSHAPE_CONTEXT;

    /**
     * Request a face selection from Onshape
     * This is called on initialization and after "Send to BionicsCAM"
     */
    function requestFaceSelection() {
        selectionRequestCounter++;
        isWaitingForSelection = true;
        const selectionMessage = {
            messageName: 'requestSelection',
            messageId: 'bionicscam-selection-' + selectionRequestCounter,
            documentId: context.documentId,
            workspaceId: context.workspaceId,
            elementId: context.elementId,
            filterType: 'simple',
            entityTypeSpecifier: ['FACE'],      // Only faces
            bodyTypeSpecifier: ['SOLID'],       // Only from solid bodies (not drawings)
            requiredSelectionCount: 1           // Exactly one face
        };
        window.parent.postMessage(selectionMessage, '*');
        console.log('Requested face selection:', selectionMessage);
    }

    /**
     * Initialize the extension
     * Send applicationInit message to Onshape
     */
    function initialize() {
        console.log('BionicsCAM panel initializing...', context);

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

        // Request initial face selection
        // This will be called again after each successful selection
        requestFaceSelection();

        // Set up button handlers
        sendBtn.addEventListener('click', handleSendToBionicsCAM);
        selectAnotherBtn.addEventListener('click', handleSelectAnother);

        // Set up mode checkbox handler
        multilayerCheckbox.addEventListener('change', updateModeInstructions);

        // Initialize mode instructions
        updateModeInstructions();
    }

    /**
     * Update instruction text based on multilayer mode
     */
    function updateModeInstructions() {
        const isMultilayer = multilayerCheckbox.checked;

        if (isMultilayer) {
            // 2.5D mode - stock must match CAD
            mode2DLabel.classList.remove('active');
            mode25DLabel.classList.add('active');
            modeHint.textContent = 'Stock thickness must match CAD part thickness';
            // Update instruction if no face selected
            if (!selectedFaceId && instruction.style.display !== 'none') {
                instruction.innerHTML = 'Select a face at the <strong>top-most layer</strong> to manufacture';
                instruction.style.color = '';
            }
        } else {
            // 2D mode - any stock works
            mode2DLabel.classList.add('active');
            mode25DLabel.classList.remove('active');
            modeHint.textContent = 'Any stock thickness works - cutting a flat pattern only';
            // Update instruction if no face selected
            if (!selectedFaceId && instruction.style.display !== 'none') {
                instruction.innerHTML = 'Select the <strong>top face</strong> to manufacture';
                instruction.style.color = '';
            }
        }
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

        if (data.messageName === 'REQUESTED_SELECTION') {
            handleRequestedSelection(data);
        } else if (data.messageName === 'SELECTION') {
            // Generic selection messages can indicate timeout
            handleGenericSelection(data);
        }
    }

    /**
     * Handle generic SELECTION messages
     */
    function handleGenericSelection(data) {
        const selections = data.selections || [];

        if (selections.length > 0) {
            // User selected something - treat it as a valid face selection
            const faceSelection = selections[0];
            selectedFaceId = faceSelection.selectionId || faceSelection.faceId || faceSelection.id || 'selected';
            selectedPartId = faceSelection.partId || null;
            currentSelection = faceSelection;
            isWaitingForSelection = false;

            instruction.innerHTML = '✓ Onshape faceId selected: <strong>' + selectedFaceId + '</strong>';
            instruction.style.color = '#27ae60';
            instruction.style.display = 'block';
            buttonGroup.style.display = 'flex';
            sendBtn.disabled = false;

            console.log('✓ Face selected via SELECTION:', selectedFaceId, 'Part:', selectedPartId);
        } else if (isWaitingForSelection && selections.length === 0) {
            // Selection request timed out - re-issue it
            console.log('Selection request timed out, re-requesting...');
            requestFaceSelection();
        } else if (!isWaitingForSelection && currentSelection) {
            // User deselected - clear and re-request
            console.log('User changed selection, requesting new face selection...');
            selectedFaceId = null;
            selectedPartId = null;
            currentSelection = null;
            buttonGroup.style.display = 'none';
            sendBtn.disabled = true;
            requestFaceSelection();
        }
    }

    /**
     * Handle requested selection response from Onshape
     */
    function handleRequestedSelection(data) {
        const selections = data.selections || [];
        const status = data.status || {};
        console.log('Requested selection response:', selections, 'Status:', status);

        // Check status code
        if (status.statusCode === 'SUCCESS' && selections.length > 0) {
            // User successfully selected a face (with filters enforced by Onshape)
            const faceSelection = selections[0];

            selectedFaceId = faceSelection.selectionId;
            selectedPartId = faceSelection.partId || null;
            currentSelection = faceSelection;
            isWaitingForSelection = false;

            // Update UI - show selected face info and buttons
            instruction.innerHTML = '✓ Onshape faceId selected: <strong>' + selectedFaceId + '</strong>';
            instruction.style.color = '#27ae60';
            instruction.style.display = 'block';
            buttonGroup.style.display = 'flex';
            sendBtn.disabled = false;

            console.log('✓ Face selected:', selectedFaceId, 'Part:', selectedPartId, 'Full selection:', faceSelection);
        } else if (status.statusCode === 'PENDING') {
            // Still waiting for selection
            instruction.innerHTML = 'Select a face to manufacture';
            instruction.style.color = '';
            instruction.style.display = 'block';
            buttonGroup.style.display = 'none';
            sendBtn.disabled = true;
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

        // Add multilayer mode
        const isMultilayer = multilayerCheckbox.checked;
        params.append('multilayer', isMultilayer ? 'true' : 'false');

        return `${context.baseUrl}${endpoint}?${params.toString()}`;
    }

    /**
     * Handle "Send to BionicsCAM" button
     * Opens full BionicsCAM interface in new window and requests another selection
     */
    function handleSendToBionicsCAM() {
        const url = buildUrl('/onshape/import');
        console.log('Opening BionicsCAM:', url);

        // Open in new tab (without window features to make it a tab, not popup)
        window.open(url, '_blank');

        // Immediately request another face selection for the next operation
        // This creates a select-then-send workflow
        requestFaceSelection();
    }

    /**
     * Handle "Select another face" button
     * Clears current selection and requests a new one
     */
    function handleSelectAnother() {
        console.log('User requested to select another face');

        // Clear current selection
        selectedFaceId = null;
        selectedPartId = null;
        currentSelection = null;

        // Request a new selection
        requestFaceSelection();
    }

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
})();
