(function() {
    'use strict';

    // State
    let selectedFaceId = null;
    let selectedPartId = null;

    // DOM elements
    const instruction = document.getElementById('instruction');
    const buttonGroup = document.getElementById('buttonGroup');
    const sendBtn = document.getElementById('sendToPenguinCAM');

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

        // Request current selection state (in case face is already selected)
        // Onshape will respond with a SELECTION message
        // Do this AFTER listener is set up so we can receive the response
        window.parent.postMessage({ messageName: 'getSelections' }, '*');
        console.log('Requested current selections');

        // Set up button handlers
        sendBtn.addEventListener('click', handleSendToPenguinCAM);
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
            // Check if this is a face from a solid body (not a sketch)
            // Solid body faces have a partId, sketch faces don't
            if (!faceSelection.partId) {
                // This is a sketch face, not a solid body face
                console.log('✗ Sketch face detected (no partId):', faceSelection);

                selectedFaceId = null;
                selectedPartId = null;
                buttonGroup.style.display = 'none';
                sendBtn.disabled = true;

                instruction.innerHTML = '⚠️ You selected a <strong>sketch face</strong>.<br>Please select a face from a <strong>solid body</strong> instead.<br><br>💡 Tip: If you see sketch lines, you\'re selecting from the sketch.<br>Close the sketch and select the actual 3D part face.';
                instruction.style.color = '#FBB515';
                instruction.style.display = 'block';

                return;
            }

            // Valid solid body face selected
            selectedFaceId = faceSelection.selectionId;
            selectedPartId = faceSelection.partId;

            // Update UI - hide instruction, show button
            instruction.style.display = 'none';
            buttonGroup.style.display = 'flex';

            // Enable button
            sendBtn.disabled = false;

            console.log('✓ Valid solid body face selected:', selectedFaceId, 'Part:', selectedPartId);
        } else {
            // No valid face - reset state
            selectedFaceId = null;
            selectedPartId = null;
            buttonGroup.style.display = 'none';
            sendBtn.disabled = true;

            // Check for common mistakes and show helpful message
            if (selections.length === 0) {
                // Nothing selected
                instruction.innerHTML = 'Select a face to export';
                instruction.style.color = '';
            } else {
                // Something selected, but not a face - provide helpful guidance
                const selection = selections[0];
                const entityType = selection.entityType;

                console.log('✗ Invalid selection:', entityType);

                if (entityType && entityType.startsWith('SKETCH')) {
                    // User selected part of a sketch
                    instruction.innerHTML = '⚠️ You selected a sketch element.<br>Please select a <strong>face of a solid part</strong> instead.';
                    instruction.style.color = '#FBB515';
                } else if (entityType === 'EDGE') {
                    // User selected an edge
                    instruction.innerHTML = '⚠️ You selected an edge.<br>Please select a <strong>flat face</strong> instead.';
                    instruction.style.color = '#FBB515';
                } else if (entityType === 'VERTEX') {
                    // User selected a vertex/point
                    instruction.innerHTML = '⚠️ You selected a vertex.<br>Please select a <strong>flat face</strong> instead.';
                    instruction.style.color = '#FBB515';
                } else if (entityType === 'BODY') {
                    // User selected entire body
                    instruction.innerHTML = '⚠️ You selected an entire body.<br>Please select a <strong>single flat face</strong> instead.';
                    instruction.style.color = '#FBB515';
                } else if (entityType === 'MATE_CONNECTOR') {
                    // User selected a mate connector
                    instruction.innerHTML = '⚠️ You selected a mate connector.<br>Please select a <strong>flat face</strong> instead.';
                    instruction.style.color = '#FBB515';
                } else {
                    // Unknown entity type
                    instruction.innerHTML = `⚠️ Invalid selection (${entityType}).<br>Please select a <strong>flat face of a solid part</strong>.`;
                    instruction.style.color = '#FBB515';
                }
            }

            instruction.style.display = 'block';
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

    // Initialize when DOM is ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
})();
