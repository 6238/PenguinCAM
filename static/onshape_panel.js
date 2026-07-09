(function() {
    'use strict';

    // State
    let selectedFaceId = null;
    let selectedPartId = null;
    let currentSelection = null;
    let selectedSelections = [];
    let selectionRequestCounter = 0;
    let isWaitingForSelection = false;

    // DOM elements
    const instruction = document.getElementById('instruction');
    const buttonGroup = document.getElementById('buttonGroup');
    const sendBtn = document.getElementById('sendToBionicsCAM'); // Optional now
    const selectAnotherBtn = document.getElementById('selectAnotherFace');
    const multilayerCheckbox = document.getElementById('multilayerMode');
    const mode2DLabel = document.getElementById('mode2DLabel');
    const mode25DLabel = document.getElementById('mode25DLabel');
    const modeHint = document.getElementById('modeHint');
    const multiPartGroup = document.getElementById('multiPartGroup');
    const importAllPartsBtn = document.getElementById('importAllParts');

    // Onshape context from template
    const context = window.ONSHAPE_CONTEXT;

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
            entityTypeSpecifier: ['FACE'],
            bodyTypeSpecifier: ['SOLID'],
            requiredSelectionCount: 1
        };

        window.parent.postMessage(selectionMessage, '*');
        console.log('Requested face selection:', selectionMessage);
    }

    function initialize() {
        console.log('BionicsCAM panel initializing...', context);

        const initMessage = {
            messageName: 'applicationInit',
            documentId: context.documentId,
            workspaceId: context.workspaceId,
            elementId: context.elementId
        };

        window.parent.postMessage(initMessage, '*');
        console.log('Sent applicationInit:', initMessage);

        window.addEventListener('message', handleMessage);

        requestFaceSelection();

        // Send button is optional now because it was removed from the HTML
        if (sendBtn) {
            sendBtn.addEventListener('click', handleSendToBionicsCAM);
        }

        if (selectAnotherBtn) {
            selectAnotherBtn.addEventListener('click', handleSelectAnother);
        }

        if (importAllPartsBtn) {
            importAllPartsBtn.addEventListener('click', handleImportAllParts);
        }

        if (multilayerCheckbox) {
            multilayerCheckbox.addEventListener('change', updateModeInstructions);
        }

        updateModeInstructions();
    }

    function updateModeInstructions() {
        const isMultilayer = multilayerCheckbox && multilayerCheckbox.checked;

        if (isMultilayer) {
            mode2DLabel.classList.remove('active');
            mode25DLabel.classList.add('active');
            modeHint.textContent = 'Stock thickness must match CAD part thickness';

            if (multiPartGroup) {
                multiPartGroup.style.display = 'flex';
            }

            if (importAllPartsBtn) {
                importAllPartsBtn.textContent = getSelectedFaceIds().length >= 1
                    ? '⬆ Import selected part(s) as 2.5D'
                    : '⬆ Import all parts as 2.5D';
            }

            if (!selectedFaceId && instruction.style.display !== 'none') {
                instruction.innerHTML = 'Select a face at the <strong>top-most layer</strong> to manufacture, or import all parts';
                instruction.style.color = '';
            }
        } else {
            mode2DLabel.classList.add('active');
            mode25DLabel.classList.remove('active');
            modeHint.textContent = 'Any stock thickness works - cutting a flat pattern only';

            if (multiPartGroup) {
                multiPartGroup.style.display = 'flex';
            }

            if (importAllPartsBtn) {
                importAllPartsBtn.textContent = getSelectedFaceIds().length >= 1
                    ? '⬆ Import selected part(s) as 2D'
                    : '⬆ Import all parts as 2D';
            }

            if (!selectedFaceId && instruction.style.display !== 'none') {
                instruction.innerHTML = 'Select the <strong>top face</strong> to manufacture, or import all parts';
                instruction.style.color = '';
            }
        }
    }

    function handleMessage(event) {
        if (!event.origin.includes('onshape.com')) {
            console.warn('Message from invalid origin:', event.origin);
            return;
        }

        const data = event.data;
        console.log('Received message:', data);

        if (data.messageName === 'REQUESTED_SELECTION') {
            handleRequestedSelection(data);
        } else if (data.messageName === 'SELECTION') {
            handleGenericSelection(data);
        }
    }

    function extractFaceSelections(selections) {
        return (selections || []).filter(sel => {
            const entityType = String(sel.entityType || sel.selectionType || '').toUpperCase();
            const id = sel.selectionId || sel.faceId || sel.id;
            return !!id && (entityType.includes('FACE') || entityType.includes('ENTITY') || !entityType);
        });
    }

    function setCurrentSelections(selections) {
        const faceSelections = extractFaceSelections(selections);

        if (!faceSelections.length) {
            return false;
        }

        selectedSelections = faceSelections;

        const first = faceSelections[0];
        selectedFaceId = first.selectionId || first.faceId || first.id || null;
        selectedPartId = first.partId || first.bodyId || null;
        currentSelection = first;
        isWaitingForSelection = false;

        const countText = faceSelections.length === 1 ? '1 face' : `${faceSelections.length} faces`;

        instruction.innerHTML =
            `✓ Onshape selection detected: <strong>${countText}</strong>` +
            (selectedFaceId ? ` <span style="opacity:.75">(${selectedFaceId})</span>` : '');

        instruction.style.color = '#27ae60';
        instruction.style.display = 'block';

        if (buttonGroup) {
            buttonGroup.style.display = 'flex';
        }

        if (sendBtn) {
            sendBtn.disabled = !selectedFaceId;
        }

        updateModeInstructions();

        console.log('✓ Face selection list:', selectedSelections);
        return true;
    }

    function getSelectedFaceIds() {
        const ids = [];

        for (const sel of selectedSelections) {
            const entityType = String(sel.entityType || sel.selectionType || '').toUpperCase();
            const id = sel.faceId || sel.selectionId || sel.id;

            if (id && (entityType.includes('FACE') || entityType.includes('ENTITY') || !entityType) && !ids.includes(id)) {
                ids.push(id);
            }
        }

        return ids;
    }

    function getSelectedBodyIds() {
        const ids = [];

        for (const sel of selectedSelections) {
            const bodyId = sel.bodyId || sel.partId || sel.partIdForSelection || sel.occurrenceId;

            if (bodyId && !ids.includes(bodyId)) {
                ids.push(bodyId);
            }
        }

        return ids;
    }

    function getRawSelectionRecords() {
        return selectedSelections.map(sel => ({ ...sel }));
    }

    function handleGenericSelection(data) {
        const selections = data.selections || [];

        if (selections.length > 0) {
            setCurrentSelections(selections);
        } else if (isWaitingForSelection && selections.length === 0) {
            console.log('Selection request timed out, re-requesting...');
            requestFaceSelection();
        } else if (!isWaitingForSelection && currentSelection) {
            console.log('User changed selection, requesting new face selection...');

            selectedFaceId = null;
            selectedPartId = null;
            currentSelection = null;
            selectedSelections = [];

            if (buttonGroup) {
                buttonGroup.style.display = 'none';
            }

            if (sendBtn) {
                sendBtn.disabled = true;
            }

            requestFaceSelection();
        }
    }

    function handleRequestedSelection(data) {
        const selections = data.selections || [];
        const status = data.status || {};

        console.log('Requested selection response:', selections, 'Status:', status);

        if (status.statusCode === 'SUCCESS' && selections.length > 0) {
            setCurrentSelections(selections);
        } else if (status.statusCode === 'PENDING') {
            instruction.innerHTML = 'Select a face to manufacture';
            instruction.style.color = '';
            instruction.style.display = 'block';

            if (buttonGroup) {
                buttonGroup.style.display = 'none';
            }

            if (sendBtn) {
                sendBtn.disabled = true;
            }
        }
    }

    function buildUrl(endpoint) {
        const params = new URLSearchParams({
            documentId: context.documentId,
            workspaceId: context.workspaceId,
            elementId: context.elementId,
            server: context.server
        });

        if (selectedFaceId) {
            params.append('faceId', selectedFaceId);
        }

        if (selectedPartId) {
            params.append('partId', selectedPartId);
        }

        const isMultilayer = multilayerCheckbox && multilayerCheckbox.checked;
        params.append('multilayer', isMultilayer ? 'true' : 'false');

        return `${context.baseUrl}${endpoint}?${params.toString()}`;
    }

    function handleSendToBionicsCAM() {
        const url = buildUrl('/onshape/import');

        console.log('Opening BionicsCAM:', url);

        window.open(url, '_blank');

        requestFaceSelection();
    }

    function handleImportAllParts() {
        const isMultilayer = multilayerCheckbox && multilayerCheckbox.checked;
        const selectedFaceIds = getSelectedFaceIds();
        const selectedBodyIds = getSelectedBodyIds();
        const rawSelectionRecords = getRawSelectionRecords();

        const params = new URLSearchParams({
            documentId: context.documentId,
            workspaceId: context.workspaceId,
            elementId: context.elementId,
            server: context.server || 'https://cad.onshape.com',
            multilayer: isMultilayer ? 'true' : 'false',
            multi: 'true'
        });

        if (selectedFaceIds.length >= 1) {
            params.append('faceIds', selectedFaceIds.join(','));
            params.append('selectedOnly', 'true');
        }

        if (selectedBodyIds.length >= 1) {
            params.append('bodyIds', selectedBodyIds.join(','));
        }

        if (rawSelectionRecords.length >= 1) {
            params.append('rawSelections', JSON.stringify(rawSelectionRecords));
        }

        const url = `${context.baseUrl}/onshape/import?${params.toString()}`;

        console.log('Opening BionicsCAM multi-part import:', url, 'selectedFaceIds=', selectedFaceIds, 'selectedBodyIds=', selectedBodyIds, 'rawSelections=', rawSelectionRecords);

        window.open(url, '_blank');
    }

    function handleSelectAnother() {
        console.log('User requested to select another face');

        selectedFaceId = null;
        selectedPartId = null;
        currentSelection = null;
        selectedSelections = [];

        if (buttonGroup) {
            buttonGroup.style.display = 'none';
        }

        if (sendBtn) {
            sendBtn.disabled = true;
        }

        requestFaceSelection();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initialize);
    } else {
        initialize();
    }
})();
