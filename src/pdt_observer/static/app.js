    const state = {
      profiles: [],
      mode: 'single',
      currentRunId: null,
      currentSampleSetId: null,
      currentLeads: [],
      pollTimer: null,
      pollPurpose: 'harvest',
      samplePollTimer: null,
      samplePollPurpose: 'coverage',
      geometryItems: [],
      selectedGeometryItemId: null,
      map: null,
      drawnItems: null,
      marker: null,
      overviewPointLayer: null,
      overviewFootprintLayer: null,
      overviewExtentLayer: null,
      overviewBounds: null,
      sampleExtentVisible: false,
      coordinatePlacementMode: false,
      selectedCandidateOptions: [],
      pendingCoordinatePreview: null,
      geometryListTab: 'geocoded',
      themePreference: 'system',
      workflow: null,
      activeWorkspace: 'workbench',
      fullPipelineActive: false,
      tableMode: 'verified',
      tableRows: [],
      tableVisibleRows: [],
      curation: null,
      curationFilter: 'included',
      selectedCurationItemIds: new Set(),
      tableSortKey: 'facility_name',
      tableSortDirection: 'asc'
    };
    const $ = (id) => document.getElementById(id);
    const terminalStatuses = ['completed', 'failed', 'cancelled'];
    const workspaceTabs = [
      ['workbenchTab', 'workbench'],
      ['geometryTab', 'geometry'],
      ['tableTab', 'table']
    ];
    const modeButtons = ['singleMode', 'batchMode', 'campaignMode'];
    const tableModeButtons = ['tableVerifiedMode', 'tableAllMode'];
    const curationFilterButtons = [
      'curationIncludedFilter',
      'curationExcludedFilter',
      'curationAllFilter'
    ];
    const geometryQueueButtons = ['geocodedQueueTab', 'manualQueueTab'];

    function setStatusMessage(elementId, message, kind = '') {
      const element = $(elementId);
      element.textContent = message;
      element.className = `status ${kind}`.trim();
      element.setAttribute('role', kind === 'error' ? 'alert' : 'status');
      element.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
    }

    function setHelpText(elementId, message = '', kind = '') {
      const element = $(elementId);
      if (!element) return;
      element.textContent = message;
      element.className = kind ? `control-help ${kind}` : 'control-help';
    }

    function setControlEnabled(id, enabled, reason = '') {
      const element = $(id);
      if (!element) return;
      element.disabled = !enabled;
      element.setAttribute('aria-disabled', String(!enabled));
      if (enabled || !reason) {
        element.removeAttribute('title');
        delete element.dataset.disabledReason;
      } else {
        element.title = reason;
        element.dataset.disabledReason = reason;
      }
    }

    function setPressedGroup(activeId, groupIds) {
      for (const id of groupIds) {
        const active = id === activeId;
        $(id).classList.toggle('active', active);
        $(id).setAttribute('aria-pressed', String(active));
      }
    }

    function workflowStage(id) {
      return (state.workflow?.stages || []).find((stage) => stage.id === id) || null;
    }

    function workflowStageComplete(id) {
      return workflowStage(id)?.status === 'complete';
    }

    function defaultWorkflowStages() {
      return [
        {
          id: 'setup',
          label: 'Setup Harvest',
          status: 'ready',
          detail: 'Choose geography, facility type, mode, and target count.'
        },
        {
          id: 'harvest',
          label: 'Run Harvest',
          status: 'blocked',
          detail: 'Start the guided pipeline or run only the harvest stage.'
        },
        {
          id: 'qaqc',
          label: 'QAQC',
          status: 'blocked',
          detail: 'Unlocks after a harvest is available.'
        },
        {
          id: 'address',
          label: 'Address Enrichment',
          status: 'blocked',
          detail: 'Unlocks after QAQC review.'
        },
        {
          id: 'geometry',
          label: 'Geocoding / Geometry',
          status: 'blocked',
          detail: 'Unlocks after address enrichment.'
        },
        {
          id: 'sample',
          label: 'Review Dataset / Coverage',
          status: 'blocked',
          detail: 'Assemble a dataset, curate rows, and check coverage.'
        },
        {
          id: 'export',
          label: 'Export',
          status: 'blocked',
          detail: 'Download verified records after review.'
        }
      ];
    }

    function actionTarget(actionId) {
      const targets = {
        start_full_pipeline: 'runFullPipelineButton',
        run_qaqc: 'runQaqcButton',
        run_address: 'runAddressButton',
        load_geometry: 'loadApprovedButton',
        create_sample: 'createSampleButton',
        analyze_coverage: 'analyzeCoverageButton',
        run_gap_fill: 'runGapFillButton',
        export_json: state.currentSampleSetId
          ? 'downloadSampleJsonButton'
          : 'downloadVerifiedJsonButton'
      };
      return targets[actionId] || null;
    }

    function updateActionAvailability() {
      const hasRun = Boolean(state.currentRunId);
      const hasSample = Boolean(state.currentSampleSetId);
      const hasSelectedGeometry = Boolean(selectedGeometryItem());
      const hasGeometryItems = state.geometryItems.length > 0;
      const hasVisibleRows = state.tableVisibleRows.length > 0;
      const hasTableContext = hasRun || hasSample;
      const busy = state.fullPipelineActive || Boolean(state.pollTimer) || Boolean(state.samplePollTimer);
      const canRunQaqc = hasRun && !busy;
      const canRunAddress = hasRun && workflowStageComplete('qaqc') && !busy;
      const canGeocode = hasRun && workflowStageComplete('address') && !busy;
      const canCreateSample = hasRun && workflowStageComplete('qaqc') && !busy;
      const canUseSample = hasSample && !busy;
      const canExportRun = hasRun && workflowStageComplete('qaqc');
      const canLoadApproved = hasRun && workflowStageComplete('qaqc');

      setControlEnabled('runFullPipelineButton', !busy, 'A pipeline or agent stage is already running.');
      setControlEnabled('runButton', !busy, 'A pipeline or agent stage is already running.');
      setControlEnabled('runQaqcButton', canRunQaqc, hasRun
        ? 'Wait for the current job to finish.'
        : 'Start or select a harvest before QAQC.');
      setControlEnabled('runAddressButton', canRunAddress, hasRun
        ? 'Run QAQC before address enrichment.'
        : 'Start or select a harvest before address enrichment.');
      setControlEnabled('geocodeButton', canGeocode, hasRun
        ? 'Run address enrichment before geocoding.'
        : 'Start or select a harvest before geocoding.');
      setControlEnabled('copyButton', hasRun || hasGeometryItems || hasSample, 'No JSON output is loaded yet.');
      setControlEnabled('copyQaqcButton', hasRun, 'Select a run before copying its QAQC prompt.');
      setControlEnabled('downloadTranscriptButton', hasRun || hasSample, 'Select a run or review dataset first.');
      setControlEnabled('downloadJsonButton', canExportRun, 'Run QAQC before downloading verified exports.');
      setControlEnabled('downloadCsvButton', canExportRun, 'Run QAQC before downloading verified exports.');
      setControlEnabled('createSampleButton', canCreateSample, hasRun
        ? 'Run QAQC before assembling a review dataset.'
        : 'Select a run before assembling a review dataset.');
      setControlEnabled('analyzeCoverageButton', canUseSample, 'Assemble a review dataset before coverage.');
      setControlEnabled('runGapFillButton', canUseSample, 'Check coverage on a review dataset before follow-ups.');
      setControlEnabled('runSampleQaqcButton', canUseSample, 'Assemble a review dataset before repair actions.');
      setControlEnabled('runSampleAddressButton', canUseSample, 'Assemble a review dataset before repair actions.');
      setControlEnabled('downloadSampleJsonButton', hasSample, 'Assemble a review dataset before sample export.');
      setControlEnabled('downloadSampleCsvButton', hasSample, 'Assemble a review dataset before sample export.');

      setControlEnabled('loadApprovedButton', canLoadApproved, hasRun
        ? 'Run QAQC before loading approved observations.'
        : 'Select a run before loading approved observations.');
      setControlEnabled('loadAugmentedSampleButton', hasSample, 'Select or assemble a review dataset first.');
      setControlEnabled('saveFootprintButton', hasSelectedGeometry, 'Select an observation before saving a footprint.');
      setControlEnabled('skipGeometryButton', hasSelectedGeometry, 'Select an observation before skipping geometry.');
      setControlEnabled('researchFacilityButton', hasSelectedGeometry, 'Select an observation before research.');
      setControlEnabled('previewCoordinatesButton', hasSelectedGeometry, 'Select an observation before previewing coordinates.');
      setControlEnabled('searchAddressButton', hasSelectedGeometry, 'Select an observation before searching an address.');
      setControlEnabled('placePointButton', hasSelectedGeometry, 'Select an observation before placing a point.');
      setControlEnabled('useMapCenterButton', hasSelectedGeometry, 'Select an observation before placing a point.');
      setControlEnabled('saveCoordinateButton', hasSelectedGeometry, 'Select an observation before saving a coordinate.');
      setControlEnabled('showSampleExtentButton', hasGeometryItems, 'Load geometry observations before showing an extent.');
      setControlEnabled('zoomSampleExtentButton', hasGeometryItems, 'Load geometry observations before zooming to extent.');
      setControlEnabled('clearSampleExtentButton', hasGeometryItems, 'Load geometry observations before clearing extent.');
      setControlEnabled('downloadVerifiedJsonButton', canExportRun, 'Run QAQC before downloading verified exports.');
      setControlEnabled('downloadVerifiedCsvButton', canExportRun, 'Run QAQC before downloading verified exports.');
      setControlEnabled('downloadFootprintsButton', hasRun && hasGeometryItems, 'Load geometry observations before footprint export.');
      setControlEnabled('downloadSampleFootprintsButton', hasSample && hasGeometryItems, 'Load sample geometry before footprint export.');

      setControlEnabled('tableVerifiedMode', hasTableContext, 'Select a run or review dataset first.');
      setControlEnabled('tableAllMode', hasRun && !hasSample, hasSample
        ? 'Review datasets show verified rows only.'
        : 'Select a run before viewing all leads.');
      setControlEnabled('tableSearch', hasTableContext, 'Select a run or review dataset first.');
      setControlEnabled('tableClearSearchButton', Boolean($('tableSearch')?.value), 'No table search is active.');
      setControlEnabled('tableRefreshButton', hasTableContext, 'Select a run or review dataset first.');
      setControlEnabled('tableCopyButton', hasVisibleRows, 'Load visible rows before copying.');
      setControlEnabled('tableCsvButton', hasVisibleRows, 'Load visible rows before downloading CSV.');
      setControlEnabled('approveCurationButton', hasSample, 'Assemble a review dataset before approval.');
      setControlEnabled('selectVisibleButton', hasVisibleRows, 'Load visible rows before selecting them.');
      setControlEnabled('excludeSelectedButton', hasSample && state.selectedCurationItemIds.size > 0, 'Select review-dataset rows before excluding.');
      setControlEnabled('restoreSelectedButton', hasSample && state.selectedCurationItemIds.size > 0, 'Select excluded rows before restoring.');

      setHelpText('resultsActionHelp', hasRun
        ? 'Manual stages unlock as upstream work completes; the workflow button shows the safest next step.'
        : 'Start or select a harvest to unlock QAQC, address enrichment, and geocoding.');
      setHelpText('exportActionHelp', canExportRun
        ? 'Verified exports are available for this run.'
        : 'Verified exports unlock after QAQC has produced keep decisions.');
      setHelpText('geometryLoadHelp', canLoadApproved || hasSample
        ? 'Load QAQC-approved run observations or review-dataset observations for map review.'
        : 'Run QAQC first, then load approved observations for geometry review.');
      setHelpText('geometryActionHelp', hasSelectedGeometry
        ? 'Selected observation is ready for coordinate or footprint review.'
        : 'Select an observation before saving a footprint or skipping geometry review.');
      setHelpText('geometryResolverHelp', hasSelectedGeometry
        ? 'Use search, pasted coordinates, or map placement, then save the confirmed coordinate.'
        : 'Select an observation to unlock coordinate search, placement, and save actions.');
      setHelpText('tableActionHelp', hasVisibleRows
        ? `${state.tableVisibleRows.length} visible row(s) are ready to copy or download.`
        : (hasTableContext
          ? 'Refresh or switch table mode after QAQC to load rows.'
          : 'Select a run or review dataset to load table rows.'));
      setHelpText('sampleActionHelp', hasSample
        ? 'Review-dataset actions are ready; approve curated rows before coverage when prompted.'
        : (hasRun
          ? 'Assemble a review dataset after QAQC, then check coverage.'
          : 'Select a run before assembling a review dataset.'));
      setHelpText('repairActionHelp', hasSample
        ? 'Missing-stage fixes and sample exports are available for this review dataset.'
        : 'Missing-stage fixes and sample exports unlock after review-dataset assembly.');
      setHelpText('curationActionHelp', state.selectedCurationItemIds.size
        ? `${state.selectedCurationItemIds.size} row(s) selected for curation.`
        : 'Select rows only when you want to exclude or restore them; approval with no exclusions is valid.');

      const nextAction = $('workflowNextButton').dataset.action;
      const nextTarget = actionTarget(nextAction);
      const nextEnabled = nextAction === 'review_curation' || (
        nextAction && (!nextTarget || !$(nextTarget).disabled)
      );
      setControlEnabled(
        'workflowNextButton',
        Boolean(nextEnabled),
        nextAction ? 'Complete the prerequisite stage first.' : 'No next workflow action is available.'
      );
    }

    function effectiveTheme(preference) {
      if (preference === 'light' || preference === 'dark') return preference;
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }

    function applyTheme(preference) {
      state.themePreference = preference;
      document.documentElement.dataset.theme = effectiveTheme(preference);
      $('themeSelect').value = preference;
    }

    function initTheme() {
      const stored = localStorage.getItem('observationHarvesterTheme') || 'system';
      applyTheme(stored);
      window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
        if (state.themePreference === 'system') applyTheme('system');
      });
    }

    function setWorkspaceTab(workspace) {
      state.activeWorkspace = workspace;
      for (const panel of document.querySelectorAll('[data-workspace]')) {
        panel.classList.toggle('hidden', panel.dataset.workspace !== workspace);
      }
      for (const [tabId, tabWorkspace] of workspaceTabs) {
        const active = workspace === tabWorkspace;
        $(tabId).classList.toggle('active', active);
        $(tabId).setAttribute('aria-selected', String(active));
        $(tabId).setAttribute('aria-pressed', String(active));
      }
      if (workspace === 'geometry') {
        initMap();
        window.setTimeout(() => {
          if (state.map) state.map.invalidateSize();
          if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
        }, 0);
      } else if (workspace === 'table') {
        refreshDataTable().catch((error) => setTableStatus(error.message, 'error'));
      }
      updateActionAvailability();
    }

    function setPipelineStatus(heading, message, kind = '') {
      $('fullPipelineHeading').textContent = heading;
      $('fullPipelineStatus').textContent = message;
      $('fullPipelineStatus').className = kind ? `status ${kind}` : '';
    }

    function setStatus(message, kind = '') {
      setStatusMessage('status', message, kind);
    }

    function selectedProfileSet() {
      return state.profiles.find((item) => item.profile_set_id === $('profileSet').value);
    }

    function renderProfileSets() {
      $('profileSet').innerHTML = state.profiles.map((profileSet) =>
        `<option value="${profileSet.profile_set_id}">${profileSet.label}</option>`
      ).join('');
      $('campaignFacilityTypes').innerHTML = state.profiles.map((profileSet) => {
        const selected = ['schools', 'manufacturing', 'restaurants'].includes(
          profileSet.profile_set_id
        )
          ? ' selected'
          : '';
        return `<option value="${profileSet.profile_set_id}"${selected}>` +
          `${profileSet.label}</option>`;
      }).join('');
      renderProfiles();
    }

    function renderProfiles() {
      const profileSet = selectedProfileSet();
      const options = ['<option value="">All subtypes</option>'];
      if (profileSet) {
        for (const profile of profileSet.profiles) {
          options.push(`<option value="${profile.profile_id}">${profile.label}</option>`);
        }
      }
      $('profile').innerHTML = options.join('');
    }

    function setMode(mode) {
      state.mode = mode;
      const activeId = mode === 'batch'
        ? 'batchMode'
        : (mode === 'campaign' ? 'campaignMode' : 'singleMode');
      setPressedGroup(activeId, modeButtons);
      $('singleLocalityBlock').classList.toggle('hidden', mode === 'campaign');
      $('campaignLocalitiesBlock').classList.toggle('hidden', mode !== 'campaign');
      $('singleFacilityBlock').classList.toggle('hidden', mode === 'campaign');
      $('campaignFacilityBlock').classList.toggle('hidden', mode !== 'campaign');
      $('subtypeBlock').classList.toggle('hidden', mode !== 'single');
      $('profile').disabled = mode !== 'single';
      updateActionAvailability();
    }

    function splitLocalities() {
      return $('localities').value
        .split(/[\n,]/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function selectedCampaignFacilityTypes() {
      return Array.from($('campaignFacilityTypes').selectedOptions)
        .map((option) => option.value)
        .filter(Boolean);
    }

    function isTerminal(status) {
      return terminalStatuses.includes(status);
    }

    async function api(path, options = {}) {
      const response = await fetch(path, options);
      const contentType = response.headers.get('content-type') || '';
      const payload = contentType.includes('application/json')
        ? await response.json()
        : await response.text();
      if (!response.ok) {
        throw new Error(
          typeof payload === 'string' ? payload : (payload.error || 'Request failed')
        );
      }
      return payload;
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function workflowAction(actionId) {
      if (actionId === 'start_full_pipeline') {
        runFullPipeline();
        return;
      }
      if (actionId === 'review_curation') {
        setWorkspaceTab('table');
        refreshDataTable().catch((error) => setTableStatus(error.message, 'error'));
        return;
      }
      const target = actionTarget(actionId);
      if (target) {
        if (actionId === 'load_geometry') setWorkspaceTab('geometry');
        $(target).click();
      }
    }

    function renderWorkflow(payload) {
      state.workflow = payload;
      const stages = payload?.stages || defaultWorkflowStages();
      const nextAction = payload?.next_action ||
        (!state.currentRunId && !state.currentSampleSetId
          ? { id: 'start_full_pipeline', label: 'Start Full Pipeline' }
          : null);
      $('workflowSummary').textContent = nextAction
        ? `Recommended next: ${nextAction.label}`
        : (stages.length ? 'All available workflow stages are up to date.' :
          'Start or select a harvest to see the full workflow.');
      $('workflowNextButton').disabled = !nextAction;
      $('workflowNextButton').textContent = nextAction ? nextAction.label : 'Next action';
      $('workflowNextButton').dataset.action = nextAction?.id || '';
      const symbols = {
        complete: '✓',
        running: '●',
        attention: '!',
        ready: '→',
        blocked: '○'
      };
      $('workflowSteps').innerHTML = stages.map((stage) => {
        const total = Number(stage.total || 0);
        const current = Number(stage.current || 0);
        const displayMode = stage.display_mode || 'progress';
        const showProgress = ['progress', 'job_progress'].includes(displayMode) &&
          (total > 0 || stage.indeterminate);
        const percent = stage.status === 'complete'
          ? 100
          : (total > 0 ? Math.min(100, Math.round((current / total) * 100)) : 0);
        const progress = showProgress
          ? `<div class="workflow-progress"><div class="workflow-progress-fill` +
            `${stage.indeterminate ? ' indeterminate' : ''}" style="width:${percent}%"></div></div>`
          : '';
        const action = stage.action_id && ['ready', 'attention'].includes(stage.status)
          ? `<button class="secondary" type="button" data-workflow-action="` +
            `${escapeHtml(stage.action_id)}">${escapeHtml(stage.action_label)}</button>`
          : '';
        return `<div class="workflow-step ${escapeHtml(stage.status)}">
          <div class="workflow-marker">${symbols[stage.status] || '○'}</div>
          <div>
            <div class="workflow-title">${escapeHtml(stage.label)}</div>
            <div class="workflow-detail">${escapeHtml(stage.detail)}</div>
            ${progress}
          </div>
          ${action}
        </div>`;
      }).join('');
      updateActionAvailability();
    }

    function renderGeocodingProgress({
      attempted,
      total,
      geocoded,
      humanReview,
      errors,
      working = false
    }) {
      if (!state.workflow) return;
      const workflow = JSON.parse(JSON.stringify(state.workflow));
      const stage = (workflow.stages || []).find((item) => item.id === 'geometry');
      if (!stage) return;
      stage.status = 'running';
      stage.current = attempted;
      stage.total = total;
      stage.indeterminate = working;
      stage.detail =
        `${attempted}/${total} attempted; ${geocoded} positioned; ` +
        `${humanReview} need coordinate assignment; ${errors} errors.`;
      workflow.next_action = null;
      renderWorkflow(workflow);
      if (state.fullPipelineActive) {
        setPipelineStatus(
          'Step 4 of 6 - Automated geocoding',
          `${attempted}/${total} observations processed; ${geocoded} positioned; ` +
            `${humanReview} need human review.`
        );
      }
    }

    async function loadWorkflowStatus() {
      const path = state.currentSampleSetId
        ? `/api/samples/${state.currentSampleSetId}/workflow-status`
        : (state.currentRunId ? `/api/runs/${state.currentRunId}/workflow-status` : null);
      if (!path) return renderWorkflow(null);
      const payload = await api(path);
      if (payload.active) {
        const purpose = state.currentSampleSetId ? state.samplePollPurpose : state.pollPurpose;
        const activeStages = {
          harvest: 'harvest',
          qaqc: 'qaqc',
          address: 'address',
          coverage: 'coverage',
          'gap fill': 'gap_fill',
          'targeted follow-ups': 'gap_fill',
          'coverage gap follow-ups': 'gap_fill',
          'missing QAQC': 'qaqc',
          'missing address': 'address'
        };
        const activeStage = payload.stages.find((stage) => stage.id === activeStages[purpose]);
        if (activeStage && activeStage.status !== 'complete') {
          activeStage.status = 'running';
          activeStage.indeterminate = true;
          activeStage.display_mode = activeStage.id === 'gap_fill' ? 'job_progress' : 'progress';
          activeStage.detail = `${activeStage.label} agent work is currently running.`;
        }
      }
      renderWorkflow(payload);
    }

    function requestBody() {
      const countMethod = $('countMethod').value;
      if (state.mode === 'campaign') {
        const body = {
          country: $('country').value.trim(),
          localities: splitLocalities(),
          facility_types: selectedCampaignFacilityTypes(),
          target: Number($('target').value || 20)
        };
        if (countMethod) body.count_method_override = countMethod;
        return body;
      }
      const body = {
        country: $('country').value.trim(),
        locality: $('locality').value.trim() || null,
        profiles: $('profileSet').value,
        target: Number($('target').value || 20)
      };
      if (state.mode === 'single' && $('profile').value) body.profile = $('profile').value;
      if (countMethod) body.count_method_override = countMethod;
      return body;
    }

    function renderResult(manifest, leads) {
      state.currentRunId = manifest.run_id || manifest.batch_id || manifest.campaign_id || null;
      state.currentLeads = leads || [];
      const summary = manifest.summary || {};
      const grouped = Boolean(manifest.batch_id || manifest.campaign_id);
      $('metricStatus').textContent = manifest.status || '-';
      $('metricLeads').textContent = summary.budget_observation_count ??
        summary.lead_count ?? leads.length ?? 0;
      $('metricFacilityLabel').textContent = grouped ? 'Completed' : 'Facility';
      $('metricAggregateLabel').textContent = grouped ? 'Failed' : 'Aggregate';
      $('metricFacility').textContent = grouped
        ? (summary.completed_count || 0)
        : (summary.facility_level_count || 0);
      $('metricAggregate').textContent = grouped
        ? (summary.failed_count || 0)
        : (summary.regional_aggregate_count || 0);
      $('jsonOutput').value = JSON.stringify(leads.length ? leads : { manifest }, null, 2);
      setTableBadge();
      updateActionAvailability();
    }

    function resetResults() {
      stopPolling();
      stopSamplePolling();
      state.currentRunId = null;
      state.currentSampleSetId = null;
      state.currentLeads = [];
      state.geometryItems = [];
      state.selectedGeometryItemId = null;
      state.tableRows = [];
      state.tableVisibleRows = [];
      $('metricStatus').textContent = '-';
      $('metricLeads').textContent = '0';
      $('metricFacilityLabel').textContent = 'Facility';
      $('metricAggregateLabel').textContent = 'Aggregate';
      $('metricFacility').textContent = '0';
      $('metricAggregate').textContent = '0';
      $('jsonOutput').value = '';
      $('sampleOutput').value = '';
      $('activityOutput').value = '';
      $('dialogueOutput').value = '';
      $('geometryDetail').value = '';
      renderGeometryList();
      renderWorkflow(null);
      resetTable('No tabular data loaded.');
      updateActionAvailability();
    }

    async function loadLog(runId) {
      if (!runId) return;
      const response = await fetch(`/api/runs/${runId}/log`);
      $('activityOutput').value = response.ok ? await response.text() : await response.text();
      $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
    }

    function transcriptPath(download = false) {
      if (state.currentSampleSetId) {
        return `/api/samples/${state.currentSampleSetId}/` +
          (download ? 'transcript.txt' : 'dialogue');
      }
      if (state.currentRunId) {
        return `/api/runs/${state.currentRunId}/` +
          (download ? 'transcript.txt' : 'dialogue');
      }
      return null;
    }

    async function loadDialogue() {
      const path = transcriptPath(false);
      if (!path) return;
      const response = await fetch(path);
      $('dialogueOutput').value = response.ok ? await response.text() : '';
      $('dialogueOutput').scrollTop = $('dialogueOutput').scrollHeight;
    }

    async function downloadTranscript() {
      const path = transcriptPath(true);
      if (!path) return setStatus('No pipeline selected.', 'error');
      const response = await fetch(path);
      if (!response.ok) return setStatus(await response.text(), 'error');
      const identity = state.currentSampleSetId || state.currentRunId;
      downloadText(
        `${identity}-pipeline-transcript.txt`,
        await response.text(),
        'text/plain'
      );
    }

    function stopPolling() {
      if (state.pollTimer) window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.pollPurpose = 'harvest';
      $('cancelButton').disabled = true;
      updateActionAvailability();
    }

    async function pollCurrentRun() {
      if (!state.currentRunId) return;
      const payload = await api(`/api/runs/${state.currentRunId}/status`);
      const manifest = payload.manifest;
      let leads = state.currentLeads;
      if (manifest.run_id && manifest.validation_valid) {
        try {
          leads = (await api(`/api/runs/${state.currentRunId}/leads`)).leads;
        } catch (_) {
          leads = [];
        }
      }
      renderResult(manifest, leads);
      $('cancelButton').disabled = !payload.active;
      updateActionAvailability();
      await loadLog(state.currentRunId);
      await loadDialogue(state.currentRunId);
      await loadWorkflowStatus();
      if (state.pollPurpose === 'qaqc') {
        if (payload.active) {
          const stamp = new Date().toLocaleTimeString();
          const heartbeat = `\\n[local ${stamp}] QAQC still running...\\n`;
          if (!$('activityOutput').value.endsWith(heartbeat)) {
            $('activityOutput').value += heartbeat;
            $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
          }
          setStatus('QAQC still running. Watching agent activity...', 'ok');
        }
        if (!payload.active) {
          stopPolling();
          const reviews = await api(`/api/runs/${state.currentRunId}/qaqc-reviews`);
          $('jsonOutput').value = JSON.stringify(reviews, null, 2);
          $('metricStatus').textContent = 'qaqc complete';
          $('metricLeads').textContent = reviews.review_count || 0;
          $('metricFacilityLabel').textContent = 'Children';
          $('metricAggregateLabel').textContent = 'Reviews';
          $('metricFacility').textContent = (reviews.child_reviews || []).length;
          $('metricAggregate').textContent = reviews.review_count || 0;
          if (state.activeWorkspace === 'table') await refreshDataTable();
          setStatus('QAQC complete.', 'ok');
        }
        return payload;
      }
      if (state.pollPurpose === 'address') {
        if (payload.active) {
          const stamp = new Date().toLocaleTimeString();
          const heartbeat = `\\n[local ${stamp}] Address enrichment still running...\\n`;
          if (!$('activityOutput').value.endsWith(heartbeat)) {
            $('activityOutput').value += heartbeat;
            $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
          }
          setStatus('Address enrichment still running. Watching agent activity...', 'ok');
        }
        if (!payload.active) {
          stopPolling();
          const results = await api(`/api/runs/${state.currentRunId}/address-results`);
          $('jsonOutput').value = JSON.stringify(results, null, 2);
          $('metricStatus').textContent = 'address complete';
          $('metricLeads').textContent = results.result_count || 0;
          $('metricFacilityLabel').textContent = 'Children';
          $('metricAggregateLabel').textContent = 'Addresses';
          $('metricFacility').textContent = (results.child_results || []).length;
          $('metricAggregate').textContent = results.result_count || 0;
          if (state.activeWorkspace === 'table') await refreshDataTable();
          setStatus('Address enrichment complete.', 'ok');
        }
        return payload;
      }
      if (isTerminal(manifest.status)) {
        stopPolling();
        setStatus(
          manifest.status === 'completed' ? 'Harvest complete.' : `Harvest ${manifest.status}.`,
          manifest.status === 'completed' ? 'ok' : 'error'
        );
        await loadRuns();
      }
      return payload;
    }

    function startPolling(runId, purpose = 'harvest') {
      stopPolling();
      state.currentRunId = runId;
      state.pollPurpose = purpose;
      state.pollTimer = window.setInterval(() => {
        pollCurrentRun().catch((error) => setStatus(error.message, 'error'));
      }, 1500);
      updateActionAvailability();
      pollCurrentRun().catch((error) => setStatus(error.message, 'error'));
    }

    async function runHarvest(options = {}) {
      const managed = Boolean(options.managed);
      const button = $('runButton');
      button.disabled = true;
      state.currentSampleSetId = null;
      state.geometryItems = [];
      state.selectedGeometryItemId = null;
      renderGeometryList();
      setStatus('Preparing geographic vernacular review...');
      try {
        const body = requestBody();
        const geographerRequest = state.mode === 'campaign'
          ? {
              country: body.country,
              localities: body.localities,
              facility_types: body.facility_types,
              mode: 'campaign'
            }
          : {
              country: body.country,
              locality: body.locality,
              profiles: body.profiles,
              profile: state.mode === 'single' ? (body.profile || null) : null,
              mode: state.mode
            };
        const geographerPayload = await api('/api/geographer/plan', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(geographerRequest)
        });
        body.geographer_plan_path = geographerPayload.plan_path;
        if (state.mode === 'single') {
          body.run_id = geographerPayload.run_id;
        } else if (state.mode === 'batch') {
          body.batch_id = geographerPayload.run_id;
        } else {
          body.campaign_id = geographerPayload.run_id;
        }
        $('dialogueOutput').value = geographerPayload.dialogue || '';
        $('dialogueOutput').scrollTop = $('dialogueOutput').scrollHeight;
        setStatus(
          geographerPayload.plan.status === 'fallback'
            ? 'Geographer used the safe fallback. Starting harvest...'
            : 'Geographer prepared local terminology. Starting harvest...',
          'ok'
        );
        const endpoint = state.mode === 'campaign'
          ? '/api/harvest/campaign-run'
          : (state.mode === 'batch' ? '/api/harvest/batch-run' : '/api/harvest/run');
        const payload = await api(endpoint, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body)
        });
        renderResult(payload.manifest, payload.leads || []);
        await loadLog(state.currentRunId);
        await loadDialogue(state.currentRunId);
        const failed = payload.manifest.status === 'failed';
        setStatus(
          isTerminal(payload.manifest.status)
            ? (failed ? 'Harvest failed. See manifest output.' : 'Harvest complete.')
            : 'Harvest started. Watching agent activity...',
          failed ? 'error' : 'ok'
        );
        if (!isTerminal(payload.manifest.status) && !managed) {
          startPolling(state.currentRunId);
        }
        await loadRuns();
        return payload;
      } catch (error) {
        setStatus(error.message, 'error');
        if (managed) throw error;
        return null;
      } finally {
        updateActionAvailability();
      }
    }

    async function loadRuns() {
      const payload = await api('/api/runs');
      $('history').innerHTML = payload.runs.slice().reverse().map((run) => {
        const id = run.run_id || run.batch_id || run.campaign_id;
        const label = run.manifest_type === 'campaign'
          ? 'Campaign'
          : (run.manifest_type === 'batch' ? 'Batch' : 'Run');
        const scope = run.manifest_type === 'campaign'
          ? [run.country, (run.localities || []).join(', ') || 'countrywide'].join(' / ')
          : [run.country, run.locality].filter(Boolean).join(' / ');
        return `<button type="button" data-run="${id}">
          ${label}: ${id}<br>${scope} - ${run.status}
        </button>`;
      }).join('') || '<div class="status">No runs yet.</div>';
      for (const button of $('history').querySelectorAll('button[data-run]')) {
        button.addEventListener('click', () => loadRun(button.dataset.run));
      }
      updateActionAvailability();
    }

    async function clearRuns() {
      if (!window.confirm('Clear generated harvest history and lead/log/prompt files?')) {
        return;
      }
      const payload = await api('/api/runs/clear', { method: 'POST' });
      resetResults();
      await loadRuns();
      setStatus(`Cleared ${payload.deleted_files} generated file(s).`, 'ok');
      updateActionAvailability();
    }

    async function loadRun(runId) {
      const detail = await api(`/api/runs/${runId}`);
      state.currentSampleSetId = null;
      let leads = [];
      if (detail.manifest.run_id) {
        try {
          leads = (await api(`/api/runs/${runId}/leads`)).leads;
        } catch (_) {
          leads = [];
        }
      }
      renderResult(detail.manifest, leads);
      await loadLog(runId);
      await loadDialogue(runId);
      await loadWorkflowStatus();
      if (!isTerminal(detail.manifest.status)) {
        startPolling(runId);
      } else {
        stopPolling();
      }
      setStatus(`Loaded ${runId}.`);
      if (state.activeWorkspace === 'table') {
        await refreshDataTable();
      } else {
        setTableBadge();
      }
      updateActionAvailability();
    }

    const tableColumns = [
      ['select', 'Select'],
      ['curation_status', 'Dataset Status'],
      ['exclusion_reason_note', 'Exclusion Reason'],
      ['run_id', 'Run'],
      ['sample_set_id', 'Dataset'],
      ['sample_round', 'Round'],
      ['facility_type', 'Facility Type'],
      ['evidence_role', 'Evidence Role'],
      ['lead_index', 'Lead'],
      ['count_index', 'Count Row'],
      ['facility_name', 'Facility'],
      ['count', 'Count'],
      ['group_type', 'Group'],
      ['count_relationship', 'Count Relationship'],
      ['component_type', 'Component'],
      ['value', 'Component Summary'],
      ['unit', 'Unit'],
      ['time_basis', 'Time Basis'],
      ['geography_level', 'Geography Level'],
      ['component_bundle_status', 'Bundle Status'],
      ['bundle_readiness', 'Bundle Readiness'],
      ['model_ready', 'Model Ready'],
      ['bundle_review_required', 'Review Required'],
      ['counts_toward_target', 'Counts Toward Target'],
      ['missing_component_types', 'Missing Components'],
      ['source_count', 'Sources'],
      ['incident_date', 'Date'],
      ['incident_time', 'Time'],
      ['strategy_id', 'Strategy'],
      ['representativeness', 'Representativeness'],
      ['confidence', 'Confidence'],
      ['city_or_region', 'City/Region'],
      ['country', 'Country'],
      ['source_url', 'Source'],
      ['qaqc_status', 'QAQC'],
      ['recommended_action', 'Action'],
      ['address_status', 'Address'],
      ['enriched_address', 'Enriched Address'],
      ['geometry_status', 'Geometry'],
      ['area_m2', 'Area m2'],
      ['review_notes', 'Review Notes'],
      ['actions', 'Actions']
    ];

    function componentValueColumns(rows = state.tableRows) {
      const labels = [];
      const seen = new Set();
      rows.forEach((row) => {
        const values = row.component_values || {};
        if (!values || typeof values !== 'object' || Array.isArray(values)) return;
        Object.keys(values).forEach((label) => {
          if (seen.has(label)) return;
          seen.add(label);
          labels.push(label);
        });
      });
      return labels.sort((left, right) => left.localeCompare(right))
        .map((label) => [`component_values.${label}`, label]);
    }

    function countValueColumns(rows = state.tableRows) {
      const labels = [];
      const seen = new Set();
      rows.forEach((row) => {
        const values = row.count_values || {};
        if (!values || typeof values !== 'object' || Array.isArray(values)) return;
        Object.keys(values).forEach((label) => {
          if (seen.has(label)) return;
          seen.add(label);
          labels.push(label);
        });
      });
      return labels.sort((left, right) => left.localeCompare(right))
        .map((label) => [`count_values.${label}`, label.replace(/^count_/, 'Count: ')]);
    }

    function activeTableColumns() {
      const columns = [...tableColumns];
      const insertIndex = columns.findIndex(([key]) => key === 'incident_date');
      const dynamicColumns = [...countValueColumns(), ...componentValueColumns()];
      if (insertIndex === -1 || !dynamicColumns.length) return columns;
      columns.splice(insertIndex, 0, ...dynamicColumns);
      return columns;
    }

    function setTableStatus(message, kind = '') {
      setStatusMessage('tableStatus', message, kind);
    }

    function tableContextLabel() {
      if (state.currentSampleSetId) return `Review dataset: ${state.currentSampleSetId}`;
      if (state.currentRunId) return `Run: ${state.currentRunId}`;
      return 'No run or review dataset selected';
    }

    function setTableBadge() {
      $('tableTabBadge').textContent = state.tableRows.length;
      $('tableTabBadge').title = `${state.tableRows.length} loaded table row(s)`;
    }

    function resetTable(
      message = 'Select a run or review dataset to inspect collected observations.'
    ) {
      state.tableRows = [];
      state.tableVisibleRows = [];
      state.curation = null;
      state.selectedCurationItemIds.clear();
      $('curationPanel').classList.add('hidden');
      $('tableContext').textContent = tableContextLabel();
      $('tableMetricContext').textContent = state.currentSampleSetId
        ? 'sample'
        : (state.currentRunId ? 'run' : '-');
      $('tableMetricMode').textContent = state.tableMode === 'all' ? 'All' : 'Verified';
      $('tableMetricRows').textContent = '0';
      $('tableMetricVisible').textContent = '0';
      $('tableHead').innerHTML = '';
      $('tableBody').innerHTML = '';
      $('tableWrap').classList.add('hidden');
      $('tableEmpty').classList.remove('hidden');
      $('tableEmpty').textContent = message;
      setTableBadge();
      updateActionAvailability();
    }

    function setTableMode(mode) {
      state.tableMode = mode === 'all' ? 'all' : 'verified';
      setPressedGroup(
        state.tableMode === 'all' ? 'tableAllMode' : 'tableVerifiedMode',
        tableModeButtons
      );
      refreshDataTable().catch((error) => setTableStatus(error.message, 'error'));
    }

    function tableEndpoint() {
      if (state.currentSampleSetId) {
        return `/api/samples/${state.currentSampleSetId}/table?mode=verified`;
      }
      if (state.currentRunId) {
        return `/api/runs/${state.currentRunId}/table?mode=${state.tableMode}`;
      }
      return null;
    }

    function setCurationFilter(filter) {
      state.curationFilter = ['included', 'excluded', 'all'].includes(filter)
        ? filter
        : 'included';
      $('curationIncludedFilter').classList.toggle('active', state.curationFilter === 'included');
      $('curationExcludedFilter').classList.toggle('active', state.curationFilter === 'excluded');
      $('curationAllFilter').classList.toggle('active', state.curationFilter === 'all');
      setPressedGroup(
        state.curationFilter === 'excluded'
          ? 'curationExcludedFilter'
          : (state.curationFilter === 'all' ? 'curationAllFilter' : 'curationIncludedFilter'),
        curationFilterButtons
      );
      renderDataTable();
    }

    function renderCurationSummary() {
      const summary = state.curation;
      $('curationPanel').classList.toggle('hidden', !summary);
      if (!summary) return;
      const approval = summary.approval_status === 'approved'
        ? 'Approved'
        : (summary.approval_status === 'stale' ? 'Approval needs renewal' : 'Awaiting approval');
      $('curationSummary').textContent =
        `${summary.included_count} included, ${summary.excluded_count} excluded. ${approval}. ` +
        'You may approve without selecting or excluding any observations.';
      $('curationStatus').textContent = state.selectedCurationItemIds.size
        ? `${state.selectedCurationItemIds.size} observation(s) selected.`
        : 'No observations selected. Approval with no exclusions is valid.';
      $('approveCurationButton').textContent = summary.approval_status === 'approved'
        ? 'Reapprove & Check Coverage'
        : 'Approve Dataset & Check Coverage';
      updateActionAvailability();
    }

    async function refreshDataTable() {
      const endpoint = tableEndpoint();
      $('tableContext').textContent = tableContextLabel();
      $('tableAllMode').disabled = Boolean(state.currentSampleSetId);
      if (state.currentSampleSetId && state.tableMode === 'all') {
        state.tableMode = 'verified';
        setPressedGroup('tableVerifiedMode', tableModeButtons);
      }
      if (!endpoint) {
        resetTable('No run or review dataset selected.');
        return;
      }
      setTableStatus('Loading table rows...');
      try {
        const payload = await api(endpoint);
        state.tableRows = payload.rows || [];
        state.curation = state.currentSampleSetId ? (payload.curation || null) : null;
        state.selectedCurationItemIds.clear();
        renderCurationSummary();
        $('tableMetricContext').textContent = payload.context_type || '-';
        $('tableMetricMode').textContent = payload.mode === 'all' ? 'All' : 'Verified';
        renderDataTable();
        const noun = state.tableRows.length === 1 ? 'row' : 'rows';
        setTableStatus(`Loaded ${state.tableRows.length} ${noun}.`, 'ok');
      } catch (error) {
        resetTable(
          state.tableMode === 'verified'
            ? 'Verified rows require a completed QAQC pass with keep decisions.'
            : 'No table rows are available for this context.'
        );
        setTableStatus(error.message, 'error');
      }
      updateActionAvailability();
    }

    function tableCellValue(row, key) {
      if (key.startsWith('component_values.')) {
        const label = key.slice('component_values.'.length);
        const values = row.component_values || {};
        return values && typeof values === 'object' ? (values[label] || '') : '';
      }
      if (key.startsWith('count_values.')) {
        const label = key.slice('count_values.'.length);
        const values = row.count_values || {};
        return values && typeof values === 'object' ? (values[label] || '') : '';
      }
      const value = row[key];
      if (value == null) return '';
      return String(value);
    }

    function sortedFilteredTableRows() {
      const needle = $('tableSearch').value.trim().toLowerCase();
      const filtered = state.tableRows.filter((row) => {
        if (state.curationFilter === 'included' && row.excluded_from_dataset) return false;
        if (state.curationFilter === 'excluded' && !row.excluded_from_dataset) return false;
        if (!needle) return true;
        return Object.values(row).some((value) =>
          String(value ?? '').toLowerCase().includes(needle)
        );
      });
      const direction = state.tableSortDirection === 'desc' ? -1 : 1;
      filtered.sort((left, right) => {
        const leftValue = tableCellValue(left, state.tableSortKey);
        const rightValue = tableCellValue(right, state.tableSortKey);
        const leftNumber = Number(leftValue);
        const rightNumber = Number(rightValue);
        if (
          leftValue !== '' &&
          rightValue !== '' &&
          Number.isFinite(leftNumber) &&
          Number.isFinite(rightNumber)
        ) {
          return (leftNumber - rightNumber) * direction;
        }
        return String(leftValue ?? '').localeCompare(String(rightValue ?? '')) * direction;
      });
      return filtered;
    }

    function renderDataTable() {
      state.tableVisibleRows = sortedFilteredTableRows();
      $('tableMetricRows').textContent = state.tableRows.length;
      $('tableMetricVisible').textContent = state.tableVisibleRows.length;
      setTableBadge();
      $('tableWrap').classList.toggle('hidden', !state.tableVisibleRows.length);
      $('tableEmpty').classList.toggle('hidden', Boolean(state.tableVisibleRows.length));
      if (!state.tableRows.length) {
        $('tableEmpty').textContent = state.tableMode === 'verified'
          ? 'No QAQC-approved rows are available for this context.'
          : 'No leads are available for this context.';
      } else if (!state.tableVisibleRows.length) {
        $('tableEmpty').textContent = 'No rows match the current search.';
      }
      const columns = activeTableColumns();
      $('tableHead').innerHTML = `<tr>${columns.map(([key, label]) => {
        if (key === 'select') return `<th>${label}</th>`;
        if (key === 'actions') return `<th>${label}</th>`;
        const marker = state.tableSortKey === key
          ? (state.tableSortDirection === 'asc' ? ' ▲' : ' ▼')
          : '';
        return `<th><button type="button" data-table-sort="${key}">` +
          `${escapeHtml(label + marker)}</button></th>`;
      }).join('')}</tr>`;
      $('tableBody').innerHTML = state.tableVisibleRows.map((row) => {
        const cells = columns.map(([key]) => {
          if (key === 'select') {
            const checked = state.selectedCurationItemIds.has(row.item_id) ? ' checked' : '';
            return `<td class="selection-cell"><input type="checkbox" ` +
              `data-curation-item="${escapeHtml(row.item_id || '')}"${checked}></td>`;
          }
          if (key === 'curation_status') {
            return `<td>${row.excluded_from_dataset ? 'Excluded' : 'Included'}</td>`;
          }
          if (key === 'exclusion_reason_note') {
            const reason = [row.exclusion_reason_code, row.exclusion_reason_note]
              .filter(Boolean).join(': ');
            return `<td>${escapeHtml(reason)}</td>`;
          }
          if (key === 'actions') {
            if (row.evidence_role === 'component_input') return '<td></td>';
            return `<td><button class="row-action" type="button" ` +
              `data-table-open-geometry="${escapeHtml(row.item_id || '')}">Open</button></td>`;
          }
          if (key === 'source_url' && /^https?:\/\//i.test(tableCellValue(row, key))) {
            const url = tableCellValue(row, key);
            return `<td><a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">` +
              `${escapeHtml(url)}</a></td>`;
          }
          return `<td>${escapeHtml(tableCellValue(row, key))}</td>`;
        }).join('');
        const rowClass = row.excluded_from_dataset ? ' class="excluded"' : '';
        return `<tr${rowClass} data-row-id="${escapeHtml(row.row_id || '')}">${cells}</tr>`;
      }).join('');
      renderCurationSummary();
      updateActionAvailability();
    }

    function csvEscape(value) {
      const text = String(value ?? '');
      if (/[",\n\r]/.test(text)) return `"${text.replaceAll('"', '""')}"`;
      return text;
    }

    function tableRowsToCsv(rows) {
      const columns = activeTableColumns().filter(([key]) => !['actions', 'select'].includes(key));
      const header = columns.map(([, label]) => csvEscape(label)).join(',');
      const lines = rows.map((row) =>
        columns.map(([key]) => csvEscape(tableCellValue(row, key))).join(',')
      );
      return [header, ...lines].join('\n');
    }

    async function copyVisibleTableRows() {
      if (!state.tableVisibleRows.length) {
        return setTableStatus('No visible rows to copy.', 'error');
      }
      await navigator.clipboard.writeText(tableRowsToCsv(state.tableVisibleRows));
      setTableStatus(`Copied ${state.tableVisibleRows.length} visible row(s).`, 'ok');
    }

    function downloadVisibleTableCsv() {
      if (!state.tableVisibleRows.length) {
        return setTableStatus('No visible rows to download.', 'error');
      }
      const identity = state.currentSampleSetId || state.currentRunId || 'table';
      downloadText(
        `${identity}.${state.tableMode}.table.csv`,
        tableRowsToCsv(state.tableVisibleRows),
        'text/csv'
      );
      setTableStatus(`Downloaded ${state.tableVisibleRows.length} visible row(s).`, 'ok');
    }

    function selectedCurationItems() {
      return Array.from(state.selectedCurationItemIds);
    }

    async function excludeSelectedObservations() {
      const itemIds = selectedCurationItems();
      if (!itemIds.length) {
        return setTableStatus('Select at least one observation to exclude.', 'error');
      }
      const reasonCode = $('exclusionReason').value;
      const reasonNote = $('exclusionNote').value.trim();
      if (reasonCode === 'other' && !reasonNote) {
        return setTableStatus('A short note is required for the Other reason.', 'error');
      }
      await api(`/api/samples/${state.currentSampleSetId}/curation/exclude`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          item_ids: itemIds,
          reason_code: reasonCode,
          reason_note: reasonNote || null
        })
      });
      $('exclusionNote').value = '';
      setTableStatus(`Excluded ${itemIds.length} observation(s). Approval is now required.`, 'ok');
      await refreshDataTable();
      await loadWorkflowStatus();
      await loadDialogue();
    }

    async function restoreSelectedObservations() {
      const itemIds = selectedCurationItems();
      if (!itemIds.length) {
        return setTableStatus('Select at least one excluded observation to restore.', 'error');
      }
      await api(`/api/samples/${state.currentSampleSetId}/curation/restore`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ item_ids: itemIds })
      });
      setTableStatus(`Restored ${itemIds.length} observation(s). Approval is now required.`, 'ok');
      await refreshDataTable();
      await loadWorkflowStatus();
      await loadDialogue();
    }

    async function approveCurationAndAnalyzeCoverage() {
      if (!state.currentSampleSetId) {
        return setTableStatus('Assemble or select a review dataset first.', 'error');
      }
      const payload = await api(`/api/samples/${state.currentSampleSetId}/curation/approve`, {
        method: 'POST'
      });
      state.curation = payload.curation;
      renderCurationSummary();
      setTableStatus(
        `Approved ${payload.curation.included_count} included observation(s); ` +
          'starting coverage check.',
        'ok'
      );
      await loadDialogue();
      await analyzeCoverage();
      await loadWorkflowStatus();
    }

    async function openTableRowInGeometry(itemId) {
      if (!itemId) return setTableStatus('This row has no geometry item ID.', 'error');
      setWorkspaceTab('geometry');
      if (state.currentSampleSetId) {
        await loadAugmentedSampleGeometry();
      } else {
        await loadApprovedGeometry();
      }
      const item = state.geometryItems.find((candidate) => candidate.item_id === itemId);
      if (!item) {
        return setGeometryStatus('That row is not available in Geometry Studio yet.', 'error');
      }
      selectGeometryItem(itemId);
      setGeometryStatus('Opened table row in Geometry Studio.', 'ok');
    }

    async function cancelRun() {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const payload = await api(`/api/runs/${state.currentRunId}/cancel`, { method: 'POST' });
      setStatus(
        payload.cancelled ? 'Cancellation requested.' : payload.message,
        payload.cancelled ? 'ok' : 'error'
      );
      await pollCurrentRun();
    }

    async function copyQaqcPrompt() {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const response = await fetch(`/api/runs/${state.currentRunId}/qaqc-prompt`);
      const text = await response.text();
      if (!response.ok) return setStatus(text, 'error');
      await navigator.clipboard.writeText(text);
      setStatus('QAQC prompt copied.', 'ok');
    }

    async function runQaqc(options = {}) {
      const managed = Boolean(options.managed);
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const button = $('runQaqcButton');
      button.disabled = true;
      setStatus('Starting QAQC agent run...');
      try {
        const payload = await api(`/api/runs/${state.currentRunId}/qaqc-run`, { method: 'POST' });
        $('activityOutput').value +=
          `\\nQAQC started for ${(payload.child_run_ids || []).length || 1} child run(s).\\n`;
        setStatus(
          payload.started ? 'QAQC started. Watching agent activity...' : 'QAQC complete.',
          'ok'
        );
        if (!managed && payload.started) startPolling(state.currentRunId, 'qaqc');
        return payload;
      } catch (error) {
        setStatus(error.message, 'error');
        if (managed) throw error;
        return null;
      } finally {
        updateActionAvailability();
      }
    }

    async function runAddressEnrichment(options = {}) {
      const managed = Boolean(options.managed);
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const button = $('runAddressButton');
      button.disabled = true;
      setStatus('Starting address enrichment agent run...');
      try {
        const payload = await api(`/api/runs/${state.currentRunId}/address-run`, {
          method: 'POST'
        });
        $('activityOutput').value +=
          `\\nAddress enrichment started for ${(payload.child_run_ids || []).length || 1} ` +
          'child run(s).\\n';
        setStatus(
          payload.started
            ? 'Address enrichment started. Watching agent activity...'
            : 'Address enrichment complete.',
          'ok'
        );
        if (!managed && payload.started) startPolling(state.currentRunId, 'address');
        return payload;
      } catch (error) {
        setStatus(error.message, 'error');
        if (managed) throw error;
        return null;
      } finally {
        updateActionAvailability();
      }
    }

    function setSampleStatus(message, kind = '') {
      setStatusMessage('sampleStatus', message, kind);
    }

    function stopSamplePolling() {
      if (state.samplePollTimer) window.clearInterval(state.samplePollTimer);
      state.samplePollTimer = null;
      updateActionAvailability();
    }

    async function loadSampleLog(sampleSetId) {
      const response = await fetch(`/api/samples/${sampleSetId}/log`);
      if (response.ok) {
        $('activityOutput').value = await response.text();
        $('activityOutput').scrollTop = $('activityOutput').scrollHeight;
      }
      await loadDialogue(sampleSetId);
    }

    async function pollSampleSet() {
      if (!state.currentSampleSetId) return;
      const payload = await api(`/api/samples/${state.currentSampleSetId}/status`);
      $('sampleOutput').value = JSON.stringify(payload.sample_set, null, 2);
      await loadSampleLog(state.currentSampleSetId);
      await loadWorkflowStatus();
      if (payload.active) {
        setSampleStatus(`${state.samplePollPurpose} still running...`, 'ok');
        return payload;
      }
      stopSamplePolling();
      if (state.samplePollPurpose === 'coverage') {
        try {
          const coverage = await api(`/api/samples/${state.currentSampleSetId}/coverage-results`);
          $('sampleOutput').value = JSON.stringify(coverage, null, 2);
          setSampleStatus('Coverage check complete.', 'ok');
        } catch (error) {
          setSampleStatus(error.message, 'error');
        }
      } else {
        setSampleStatus(`${state.samplePollPurpose} complete.`, 'ok');
      }
      if (state.activeWorkspace === 'table') await refreshDataTable();
      updateActionAvailability();
      return payload;
    }

    function startSamplePolling(sampleSetId, purpose) {
      stopSamplePolling();
      state.currentSampleSetId = sampleSetId;
      state.samplePollPurpose = purpose;
      state.samplePollTimer = window.setInterval(() => {
        pollSampleSet().catch((error) => setSampleStatus(error.message, 'error'));
      }, 1500);
      updateActionAvailability();
      pollSampleSet().catch((error) => setSampleStatus(error.message, 'error'));
    }

    async function createSampleSet() {
      if (!state.currentRunId) return setSampleStatus('Select a run first.', 'error');
      const payload = await api('/api/samples/from-run', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ run_id: state.currentRunId })
      });
      state.currentSampleSetId = payload.sample_set.sample_set_id;
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(`Review dataset assembled: ${state.currentSampleSetId}.`, 'ok');
      if (state.activeWorkspace === 'table') await refreshDataTable();
      await loadWorkflowStatus();
      await loadDialogue();
      updateActionAvailability();
      return payload;
    }

    async function analyzeCoverage(options = {}) {
      const managed = Boolean(options.managed);
      if (!state.currentSampleSetId) {
        return setSampleStatus('Assemble a review dataset first.', 'error');
      }
      let geometryNote = '';
      try {
        const summaryPayload = await api(
          `/api/samples/${state.currentSampleSetId}/coverage-summary`
        );
        const summary = summaryPayload.summary || {};
        if (summary.approved_count && !summary.geocoded_count) {
          geometryNote = ' No geocoded observations yet; geometry review can improve steering.';
        } else if (summary.geocoded_count < summary.approved_count) {
          geometryNote =
            ` ${summary.geocoded_count}/${summary.approved_count} approved observations ` +
            'have geocoded points.';
        }
      } catch (_) {
        geometryNote = '';
      }
      const payload = await api(`/api/samples/${state.currentSampleSetId}/coverage-run`, {
        method: 'POST'
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(`Coverage check started.${geometryNote}`, 'ok');
      if (payload.started && !managed) {
        startSamplePolling(state.currentSampleSetId, 'coverage');
      }
      updateActionAvailability();
      return payload;
    }

    function pipelineDelay(milliseconds) {
      return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    async function waitForRunStage(purpose) {
      stopPolling();
      state.pollPurpose = purpose;
      while (state.fullPipelineActive) {
        const payload = await pollCurrentRun();
        if (!payload.active) return payload;
        await pipelineDelay(1500);
      }
      throw new Error('Full pipeline stopped.');
    }

    async function waitForSampleStage(purpose) {
      stopSamplePolling();
      state.samplePollPurpose = purpose;
      while (state.fullPipelineActive) {
        const payload = await pollSampleSet();
        if (!payload.active) return payload;
        await pipelineDelay(1500);
      }
      throw new Error('Full pipeline stopped.');
    }

    function setPipelineControlsDisabled(disabled) {
      for (const id of [
        'runFullPipelineButton',
        'runButton',
        'runQaqcButton',
        'runAddressButton',
        'geocodeButton',
        'createSampleButton',
        'analyzeCoverageButton'
      ]) {
        $(id).disabled = disabled;
      }
      updateActionAvailability();
    }

    async function runFullPipeline() {
      if (state.fullPipelineActive) return;
      state.fullPipelineActive = true;
      state.currentSampleSetId = null;
      setPipelineControlsDisabled(true);
      setWorkspaceTab('workbench');
      try {
        setPipelineStatus(
          'Step 1 of 5 - Geographic review and harvest',
          'The Geographer Agent will adapt terminology before the harvest jobs begin.'
        );
        const harvestStart = await runHarvest({ managed: true });
        if (!harvestStart) throw new Error('The harvest could not be started.');
        const harvestStatus = await waitForRunStage('harvest');
        if (harvestStatus.manifest.status !== 'completed') {
          throw new Error(`Harvest ended with status: ${harvestStatus.manifest.status}.`);
        }

        setPipelineStatus(
          'Step 2 of 5 - QAQC',
          'Reviewing every harvested observation for evidence quality and geographic scope.'
        );
        await runQaqc({ managed: true });
        await waitForRunStage('qaqc');

        setPipelineStatus(
          'Step 3 of 5 - Address enrichment',
          'Improving facility addresses before coordinate assignment.'
        );
        await runAddressEnrichment({ managed: true });
        await waitForRunStage('address');

        setPipelineStatus(
          'Step 4 of 5 - Automated geocoding',
          'Assigning spatially validated coordinates to accepted observations.'
        );
        const geocodeSummary = await geocodeAcceptedObservations();
        if (!geocodeSummary) {
          throw new Error('No accepted observations were available for geocoding.');
        }

        setPipelineStatus(
          'Step 5 of 5 - Review dataset',
          'Combining the reviewed observations into a dataset for approval.'
        );
        await createSampleSet();

        const interventionCount = Number($('interventionCount').textContent || 0);
        const reviewNote = interventionCount
          ? ` ${interventionCount} coordinate assignment(s) also need review in Geometry Studio.`
          : '';
        setPipelineStatus(
          'Review dataset ready - approval required',
          'The automated pipeline is paused in Tabular Data. Exclude unsuitable rows, ' +
            'then approve to start the coverage check. Targeted follow-ups remain a ' +
            `separate decision.${reviewNote}`,
          'ok'
        );
        setSampleStatus(
          'Review rows in Tabular Data, exclude unsuitable rows, then approve to ' +
            'start coverage check.',
          'ok'
        );
        setWorkspaceTab('table');
        await refreshDataTable();
        await loadWorkflowStatus();
      } catch (error) {
        setPipelineStatus('Full pipeline stopped', error.message, 'error');
        setStatus(error.message, 'error');
      } finally {
        stopPolling();
        stopSamplePolling();
        state.fullPipelineActive = false;
        updateActionAvailability();
      }
    }

    async function runGapFill() {
      if (!state.currentSampleSetId) {
        return setSampleStatus('Assemble a review dataset first.', 'error');
      }
      const payload = await api(`/api/samples/${state.currentSampleSetId}/gap-fill-run`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({})
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus('Targeted follow-ups started.', 'ok');
      if (state.activeWorkspace === 'table') await refreshDataTable();
      if (payload.started) startSamplePolling(state.currentSampleSetId, 'coverage gap follow-ups');
      updateActionAvailability();
    }

    async function runSampleQaqcMissing() {
      if (!state.currentSampleSetId) {
        return setSampleStatus('Assemble a review dataset first.', 'error');
      }
      const payload = await api(`/api/samples/${state.currentSampleSetId}/qaqc-missing`, {
        method: 'POST'
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(
        payload.started ? 'Missing QAQC started.' : 'Missing QAQC pass complete.',
        'ok'
      );
      if (state.activeWorkspace === 'table') await refreshDataTable();
      if (payload.started) startSamplePolling(state.currentSampleSetId, 'missing QAQC');
      updateActionAvailability();
    }

    async function runSampleAddressMissing() {
      if (!state.currentSampleSetId) {
        return setSampleStatus('Assemble a review dataset first.', 'error');
      }
      const payload = await api(`/api/samples/${state.currentSampleSetId}/address-missing`, {
        method: 'POST'
      });
      $('sampleOutput').value = JSON.stringify(payload, null, 2);
      setSampleStatus(
        payload.started ? 'Missing address enrichment started.' : 'Missing address pass complete.',
        'ok'
      );
      if (state.activeWorkspace === 'table') await refreshDataTable();
      if (payload.started) startSamplePolling(state.currentSampleSetId, 'missing address');
      updateActionAvailability();
    }

    function setGeometryStatus(message, kind = '') {
      setStatusMessage('geometryStatus', message, kind);
    }

    function setAutomatedGeocodeStatus(message, kind = '') {
      setGeometryStatus(message, kind);
      setStatus(message, kind);
    }

    function initMap() {
      if (state.map || typeof L === 'undefined') return;
      state.map = L.map('map').setView([20, 0], 2);
      const streets = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors'
      });
      const imagery = L.tileLayer(
        'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        {
          maxZoom: 19,
          attribution: 'Tiles &copy; Esri'
        }
      );
      imagery.addTo(state.map);
      L.control.layers({ Imagery: imagery, Streets: streets }).addTo(state.map);
      state.overviewFootprintLayer = new L.FeatureGroup();
      state.overviewPointLayer = new L.FeatureGroup();
      state.overviewExtentLayer = new L.FeatureGroup();
      state.map.addLayer(state.overviewFootprintLayer);
      state.map.addLayer(state.overviewPointLayer);
      state.map.addLayer(state.overviewExtentLayer);
      state.drawnItems = new L.FeatureGroup();
      state.map.addLayer(state.drawnItems);
      const drawControl = new L.Control.Draw({
        draw: {
          polygon: true,
          rectangle: false,
          polyline: false,
          circle: false,
          circlemarker: false,
          marker: false
        },
        edit: { featureGroup: state.drawnItems }
      });
      state.map.addControl(drawControl);
      state.map.on(L.Draw.Event.CREATED, (event) => {
        state.drawnItems.clearLayers();
        state.drawnItems.addLayer(event.layer);
        setGeometryStatus(
          'Footprint drawn. Select Save Footprint to calculate area and store its geometry.',
          'ok'
        );
      });
      state.map.on('click', (event) => {
        if (!state.coordinatePlacementMode) return;
        setMarker({
          latitude: event.latlng.lat,
          longitude: event.latlng.lng,
          source: 'user'
        });
        state.coordinatePlacementMode = false;
        $('map').classList.remove('placement-active');
        $('coordinateDraftStatus').textContent =
          'Draft coordinate placed. Drag the marker if needed, then select Save Coordinate.';
        setGeometryStatus('Draft coordinate placed on the map. Save it to confirm.', 'ok');
      });
    }

    function selectedGeometryItem() {
      return state.geometryItems.find((item) => item.item_id === state.selectedGeometryItemId);
    }

    function geometryRound(item) {
      return item.sample_round ? Number(item.sample_round) : 0;
    }

    function geometryRoundLabel(item) {
      const round = geometryRound(item);
      if (!round) return 'current run';
      return round === 1 ? 'round 1' : `coverage gap follow-up round ${round}`;
    }

    function geometryColor(item) {
      const round = geometryRound(item);
      if (round > 1) return '#d97706';
      if (round === 1) return '#2563eb';
      return '#16a34a';
    }

    function geometrySummary() {
      const rounds = new Set();
      let geocoded = 0;
      let footprints = 0;
      let intervention = 0;
      for (const item of state.geometryItems) {
        if (pointFromGeometry(item)) geocoded += 1;
        if (polygonFromGeometry(item)) footprints += 1;
        if (
          item.geometry_status !== 'skipped' &&
          item.geometry?.spatial_validation?.requires_human_intervention
        ) intervention += 1;
        rounds.add(geometryRoundLabel(item));
      }
      return {
        approved: state.geometryItems.length,
        geocoded,
        footprints,
        intervention,
        missing: Math.max(state.geometryItems.length - geocoded, 0),
        rounds: Array.from(rounds).join(', ') || 'none'
      };
    }

    function updateGeometrySummary() {
      const summary = geometrySummary();
      $('geometryExtentSummary').textContent =
        `Extent: ${summary.approved} approved, ${summary.geocoded} geocoded, ` +
        `${summary.footprints} footprint(s), ${summary.missing} missing point(s). ` +
        `${summary.intervention} need coordinate assignment. Rounds: ${summary.rounds}.`;
    }

    function renderInterventionQueue() {
      const items = state.geometryItems.filter(
        (item) =>
          item.geometry_status !== 'skipped' &&
          item.geometry?.spatial_validation?.requires_human_intervention
      );
      $('interventionCount').textContent = items.length;
      $('geometryTabBadge').textContent = items.length;
      $('geometryTabBadge').title = `${items.length} coordinate assignment(s) need review`;
      $('interventionList').innerHTML = items.map((item) => {
        const facility = item.lead?.location?.facility_name || item.item_id;
        const reason = item.geometry.spatial_validation.reason || 'Coordinate needs review.';
        return `<button class="secondary" type="button" data-intervention="` +
          `${escapeHtml(item.item_id)}">${escapeHtml(facility)} - ${escapeHtml(reason)}</button>`;
      }).join('') ||
        '<span class="workflow-summary">No observations currently require intervention.</span>';
      for (const button of $('interventionList').querySelectorAll('[data-intervention]')) {
        button.addEventListener('click', () => selectGeometryItem(button.dataset.intervention));
      }
    }

    function needsManualGeocoding(item) {
      if (!item || item.geometry_status === 'skipped') return false;
      if (pointFromGeometry(item)) return false;
      return (
        item.geometry_status === 'needs_review' ||
        Boolean(item.geometry?.spatial_validation?.requires_human_intervention)
      );
    }

    function geocodedGeometryItems() {
      return state.geometryItems.filter((item) => Boolean(pointFromGeometry(item)));
    }

    function manualGeometryItems() {
      return state.geometryItems.filter((item) => needsManualGeocoding(item));
    }

    function geometryItemsForActiveTab() {
      return state.geometryListTab === 'manual'
        ? manualGeometryItems()
        : geocodedGeometryItems();
    }

    function setGeometryListTab(tab) {
      state.geometryListTab = tab === 'manual' ? 'manual' : 'geocoded';
      renderGeometryList();
      updateActionAvailability();
    }

    function chooseGeometryListTabForLoadedItems() {
      state.geometryListTab = manualGeometryItems().length ? 'manual' : 'geocoded';
    }

    function renderGeometryQueueTabs() {
      const geocodedCount = geocodedGeometryItems().length;
      const manualCount = manualGeometryItems().length;
      $('geocodedQueueCount').textContent = geocodedCount;
      $('manualQueueCount').textContent = manualCount;
      $('geocodedQueueTab').classList.toggle('active', state.geometryListTab === 'geocoded');
      $('manualQueueTab').classList.toggle('active', state.geometryListTab === 'manual');
      $('geocodedQueueTab').setAttribute(
        'aria-selected',
        String(state.geometryListTab === 'geocoded')
      );
      $('manualQueueTab').setAttribute(
        'aria-selected',
        String(state.geometryListTab === 'manual')
      );
      setPressedGroup(
        state.geometryListTab === 'manual' ? 'manualQueueTab' : 'geocodedQueueTab',
        geometryQueueButtons
      );
    }

    function renderGeometryList() {
      const listedItems = geometryItemsForActiveTab();
      const emptyMessage = state.geometryListTab === 'manual'
        ? 'No observations need manual geocoding.'
        : 'No geocoded observations yet.';
      $('geometryList').innerHTML = listedItems.map((item) => {
        const lead = item.lead;
        const addressStatus = item.address_status || 'not_run';
        const label = `${lead.location.facility_name} - ${addressStatus} - ` +
          `${item.geometry_status}`;
        const active = item.item_id === state.selectedGeometryItemId ? ' active' : '';
        return `<button type="button" class="${active}" data-geometry="${item.item_id}">
          ${label}<br>${lead.location.city_or_region}, ${lead.location.country} -
          ${geometryRoundLabel(item)}
        </button>`;
      }).join('') || `<div class="status">${emptyMessage}</div>`;
      for (const button of $('geometryList').querySelectorAll('button[data-geometry]')) {
        button.addEventListener('click', () => selectGeometryItem(button.dataset.geometry));
      }
      renderInterventionQueue();
      renderGeometryQueueTabs();
      updateGeometrySummary();
      updateActionAvailability();
    }

    function pointFromGeometry(item) {
      if (item.geometry && item.geometry.point) return item.geometry.point;
      return null;
    }

    function polygonFromGeometry(item) {
      if (item.geometry && item.geometry.polygon_geojson) return item.geometry.polygon_geojson;
      return null;
    }

    function setMarker(point) {
      initMap();
      if (!state.map || !point) return;
      if (state.marker) state.map.removeLayer(state.marker);
      state.marker = L.marker(
        [point.latitude, point.longitude],
        { draggable: true }
      ).addTo(state.map);
      state.map.setView([point.latitude, point.longitude], 18);
    }

    function clearMarker() {
      if (state.marker && state.map) state.map.removeLayer(state.marker);
      state.marker = null;
    }

    function overviewPopup(item) {
      const lead = item.lead;
      const counts = (lead.occupancy_data || [])
        .map((count) => `${count.count} ${count.group_type}`)
        .join(', ');
      return `<strong>${lead.location.facility_name}</strong><br>` +
        `${lead.location.city_or_region}, ${lead.location.country}<br>` +
        `${geometryRoundLabel(item)}<br>${counts}`;
    }

    function clearSampleExtent() {
      initMap();
      if (state.overviewPointLayer) state.overviewPointLayer.clearLayers();
      if (state.overviewFootprintLayer) state.overviewFootprintLayer.clearLayers();
      if (state.overviewExtentLayer) state.overviewExtentLayer.clearLayers();
      state.overviewBounds = null;
      state.sampleExtentVisible = false;
      updateGeometrySummary();
      setGeometryStatus('Sample extent cleared.', 'ok');
      updateActionAvailability();
    }

    function renderSampleExtent(fit = false) {
      initMap();
      if (!state.map) return;
      state.sampleExtentVisible = true;
      state.overviewPointLayer.clearLayers();
      state.overviewFootprintLayer.clearLayers();
      state.overviewExtentLayer.clearLayers();
      const bounds = L.latLngBounds([]);
      let mapped = 0;
      for (const item of state.geometryItems) {
        const color = geometryColor(item);
        const point = pointFromGeometry(item);
        if (point) {
          const marker = L.circleMarker([point.latitude, point.longitude], {
            radius: 7,
            color,
            fillColor: color,
            fillOpacity: 0.8,
            weight: 2
          });
          marker.bindPopup(overviewPopup(item));
          marker.on('click', () => selectGeometryItem(item.item_id));
          marker.addTo(state.overviewPointLayer);
          bounds.extend([point.latitude, point.longitude]);
          mapped += 1;
        }
        const polygon = polygonFromGeometry(item);
        if (polygon) {
          const footprint = L.geoJSON(polygon, {
            style: {
              color,
              fillColor: color,
              fillOpacity: 0.16,
              weight: 2
            }
          });
          footprint.bindPopup(overviewPopup(item));
          footprint.on('click', () => selectGeometryItem(item.item_id));
          footprint.addTo(state.overviewFootprintLayer);
          const footprintBounds = footprint.getBounds();
          if (footprintBounds.isValid()) bounds.extend(footprintBounds);
          mapped += 1;
        }
      }
      if (!bounds.isValid()) {
        state.overviewBounds = null;
        updateGeometrySummary();
        return setGeometryStatus('No geocoded observations are available for an extent.', 'error');
      }
      state.overviewBounds = bounds;
      L.rectangle(bounds, {
        color: '#dc2626',
        fillOpacity: 0,
        dashArray: '6 6',
        weight: 2
      }).addTo(state.overviewExtentLayer);
      if (fit) state.map.fitBounds(bounds.pad(0.15));
      updateGeometrySummary();
      setGeometryStatus(`Sample extent shows ${mapped} mapped geometry layer(s).`, 'ok');
    }

    function zoomSampleExtent() {
      if (!state.sampleExtentVisible || !state.overviewBounds) renderSampleExtent(false);
      if (!state.overviewBounds || !state.overviewBounds.isValid()) {
        return setGeometryStatus('No geocoded observations are available for an extent.', 'error');
      }
      state.map.fitBounds(state.overviewBounds.pad(0.15));
      setGeometryStatus('Zoomed to sample extent.', 'ok');
    }

    function selectGeometryItem(itemId) {
      initMap();
      state.selectedGeometryItemId = itemId;
      renderGeometryList();
      const item = selectedGeometryItem();
      if (!item) {
        updateActionAvailability();
        return;
      }
      state.coordinatePlacementMode = false;
      state.pendingCoordinatePreview = null;
      $('map').classList.remove('placement-active');
      $('pastedCoordinates').value = '';
      $('coordinatePasteStatus').className = 'status';
      $('coordinatePasteStatus').textContent =
        'Preview pasted coordinates before saving them.';
      $('manualAddress').value = item.geocode_query || '';
      $('coordinateReviewNotes').value = '';
      $('coordinateDraftStatus').textContent = pointFromGeometry(item)
        ? 'This observation already has a saved coordinate.'
        : 'No coordinate change is waiting to be saved.';
      $('resolutionReason').textContent = resolutionExplanation(item);
      setResolutionLink('resolutionSourceLink', item.lead?.source_url);
      setResolutionLink(
        'resolutionAddressLink',
        item.address_enrichment?.address_source_url
      );
      updateExternalSearchLinks(item);
      renderCandidateOptions(item);
      $('geometryDetail').value = JSON.stringify({
        item_id: item.item_id,
        facility: item.lead.location.facility_name,
        query: item.geocode_query,
        enriched_address: item.address_enrichment,
        source_url: item.lead.source_url,
        counts: item.lead.occupancy_data,
        qaqc: item.qaqc_review.review_notes,
        address_status: item.address_status,
        geometry_status: item.geometry_status,
        spatial_validation: item.geometry?.spatial_validation || null,
        area_m2: item.area_m2
      }, null, 2);
      if (state.drawnItems) state.drawnItems.clearLayers();
      const point = pointFromGeometry(item);
      if (point) setMarker(point);
      else clearMarker();
      const polygon = polygonFromGeometry(item);
      if (polygon && state.drawnItems) {
        const layer = L.geoJSON(polygon).getLayers()[0];
        state.drawnItems.addLayer(layer);
        state.map.fitBounds(layer.getBounds());
      }
      updateActionAvailability();
    }

    function setResolutionLink(elementId, url) {
      const link = $(elementId);
      const usable = typeof url === 'string' && new RegExp('^https?://', 'i').test(url);
      link.classList.toggle('hidden', !usable);
      if (usable) link.href = url;
      else link.removeAttribute('href');
    }

    function facilitySearchQuery(item) {
      const location = item?.lead?.location || {};
      return [
        location.facility_name,
        item?.address_enrichment?.formatted_address,
        location.city_or_region,
        location.country
      ].filter(Boolean).join(', ');
    }

    function updateExternalSearchLinks(item) {
      const query = facilitySearchQuery(item);
      setResolutionLink(
        'googleSearchLink',
        query ? `https://www.google.com/search?q=${encodeURIComponent(query)}` : null
      );
      setResolutionLink(
        'googleMapsLink',
        query
          ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`
          : null
      );
    }

    function candidateOptionsForItem(item) {
      const validation = item?.geometry?.spatial_validation;
      const current = Array.isArray(validation?.candidate_options)
        ? validation.candidate_options
        : [];
      const initial = Array.isArray(validation?.initial_validation?.candidate_options)
        ? validation.initial_validation.candidate_options
        : [];
      const seen = new Set();
      return [...current, ...initial].filter((candidate) => {
        const key = `${candidate.latitude},${candidate.longitude}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return candidate.latitude != null && candidate.longitude != null;
      }).slice(0, 5);
    }

    function renderCandidateOptions(item) {
      state.selectedCandidateOptions = candidateOptionsForItem(item);
      $('candidateOptions').innerHTML = state.selectedCandidateOptions.map(
        (candidate, index) => {
          const confidence = ['likely', 'possible', 'conflicting'].includes(
            candidate.confidence
          ) ? candidate.confidence : 'possible';
          const reason = Array.isArray(candidate.match_summary)
            ? candidate.match_summary.join(' ')
            : (candidate.scope_reason || 'Candidate requires human review.');
          const acceptDisabled = candidate.scope_status === 'out_of_scope'
            ? ' disabled title="Outside the requested geographic scope"'
            : '';
          return `<div class="candidate-card ${confidence}">
            <div class="candidate-heading">
              <span>${escapeHtml(candidate.display_name || 'Unnamed candidate')}</span>
              <span class="candidate-badge">${escapeHtml(confidence)}</span>
            </div>
            <div class="candidate-reason">${escapeHtml(reason)}</div>
            <div class="candidate-reason">
              ${escapeHtml(candidate.provider || 'geocoder')} · score
              ${escapeHtml(candidate.score ?? '—')}
            </div>
            <div class="actions">
              <button class="secondary" type="button" data-view-candidate="${index}">
                View on map
              </button>
              <button type="button" data-accept-candidate="${index}"${acceptDisabled}>
                Accept this location
              </button>
            </div>
          </div>`;
        }
      ).join('') || `<span class="workflow-summary">
        No ranked candidates are available yet. Search a corrected address or select
        Research This Facility.
      </span>`;
      for (const button of $('candidateOptions').querySelectorAll('[data-view-candidate]')) {
        button.addEventListener('click', () => viewCandidate(Number(button.dataset.viewCandidate)));
      }
      for (
        const button of $('candidateOptions').querySelectorAll('[data-accept-candidate]')
      ) {
        button.addEventListener('click', () => {
          acceptCandidate(Number(button.dataset.acceptCandidate)).catch(
            (error) => setGeometryStatus(error.message, 'error')
          );
        });
      }
    }

    function viewCandidate(index) {
      const candidate = state.selectedCandidateOptions[index];
      if (!candidate || !state.map) return;
      state.map.setView([candidate.latitude, candidate.longitude], 17);
      $('coordinateDraftStatus').textContent =
        'Candidate centered for inspection; no coordinate has been assigned.';
      setGeometryStatus('Candidate centered on the map for review.', 'ok');
    }

    function resolutionExplanation(item) {
      const validation = item.geometry?.spatial_validation;
      if (!validation) {
        return 'Automatic coordinate assignment has not reported a validation result.';
      }
      const lines = [
        `Status: ${validation.status || 'unknown'}`,
        `Reason: ${validation.reason || 'No reason was recorded.'}`
      ];
      const initial = validation.initial_validation;
      if (initial?.reason) lines.push(`Initial result: ${initial.reason}`);
      const retry = validation.address_retry;
      if (retry?.address?.formatted_address) {
        lines.push(`Address retry: ${retry.address.formatted_address}`);
      } else if (retry?.reason) {
        lines.push(`Address retry: ${retry.reason}`);
      }
      const attempts = [
        ...(Array.isArray(initial?.attempts) ? initial.attempts : []),
        ...(Array.isArray(validation.attempts) ? validation.attempts : [])
      ];
      if (attempts.length) {
        lines.push('Automatic attempts:');
        for (const attempt of attempts) {
          lines.push(`- ${attempt.query}: ${attempt.reason || attempt.status}`);
        }
      }
      return lines.join('\\n');
    }

    async function loadApprovedGeometry() {
      if (!state.currentRunId) return setGeometryStatus('No run selected.', 'error');
      initMap();
      const payload = await api(`/api/runs/${state.currentRunId}/geometry-items`);
      state.geometryItems = payload.items || [];
      $('jsonOutput').value = JSON.stringify(state.geometryItems, null, 2);
      chooseGeometryListTabForLoadedItems();
      state.selectedGeometryItemId =
        geometryItemsForActiveTab()[0]?.item_id || state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
      else {
        clearMarker();
        if (state.drawnItems) state.drawnItems.clearLayers();
        $('geometryDetail').value = '';
        setResolutionLink('resolutionSourceLink', null);
        setResolutionLink('resolutionAddressLink', null);
        setResolutionLink('googleSearchLink', null);
        setResolutionLink('googleMapsLink', null);
        renderCandidateOptions(null);
      }
      setGeometryStatus(`Loaded ${state.geometryItems.length} QAQC-approved observation(s).`, 'ok');
      if (state.sampleExtentVisible) renderSampleExtent(false);
      updateActionAvailability();
    }

    async function loadGeometryItemsForAutomatedGeocoding() {
      const usingSample = Boolean(state.currentSampleSetId);
      if (!usingSample && !state.currentRunId) {
        setAutomatedGeocodeStatus('Select a run before geocoding.', 'error');
        return false;
      }
      const path = usingSample
        ? `/api/samples/${state.currentSampleSetId}/geometry-items`
        : `/api/runs/${state.currentRunId}/geometry-items`;
      const payload = await api(path);
      state.geometryItems = payload.items || [];
      chooseGeometryListTabForLoadedItems();
      state.selectedGeometryItemId =
        geometryItemsForActiveTab()[0]?.item_id || state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      $('jsonOutput').value = JSON.stringify(state.geometryItems, null, 2);
      setAutomatedGeocodeStatus(
        `Prepared ${state.geometryItems.length} accepted observation(s) for geocoding.`,
        'ok'
      );
      updateActionAvailability();
      return true;
    }

    async function geocodeAcceptedObservations() {
      if (!(await loadGeometryItemsForAutomatedGeocoding())) return null;
      await loadWorkflowStatus();
      return geocodeAll();
    }

    async function loadAugmentedSampleGeometry() {
      if (!state.currentSampleSetId) {
        return setGeometryStatus('No review dataset selected.', 'error');
      }
      initMap();
      const payload = await api(`/api/samples/${state.currentSampleSetId}/geometry-items`);
      state.geometryItems = payload.items || [];
      chooseGeometryListTabForLoadedItems();
      state.selectedGeometryItemId =
        geometryItemsForActiveTab()[0]?.item_id || state.geometryItems[0]?.item_id || null;
      renderGeometryList();
      if (state.selectedGeometryItemId) selectGeometryItem(state.selectedGeometryItemId);
      else {
        clearMarker();
        if (state.drawnItems) state.drawnItems.clearLayers();
        $('geometryDetail').value = '';
        setResolutionLink('resolutionSourceLink', null);
        setResolutionLink('resolutionAddressLink', null);
        setResolutionLink('googleSearchLink', null);
        setResolutionLink('googleMapsLink', null);
        renderCandidateOptions(null);
      }
      setGeometryStatus(
        `Loaded ${state.geometryItems.length} review dataset observation(s).`,
        'ok'
      );
      if (state.sampleExtentVisible) renderSampleExtent(true);
      updateActionAvailability();
    }

    function currentPointPayload(source = 'user') {
      if (!state.marker) return null;
      const latlng = state.marker.getLatLng();
      return { latitude: latlng.lat, longitude: latlng.lng, source };
    }

    function currentPolygonGeojson() {
      if (!state.drawnItems) return null;
      const layers = state.drawnItems.getLayers();
      if (!layers.length) return null;
      return layers[0].toGeoJSON().geometry;
    }

    async function geocodeSelected(queryOverride = null) {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const query = (queryOverride || item.geocode_query || '').trim();
      if (!query) return setGeometryStatus('No address query available.', 'error');
      const payload = await api('/api/geometry/geocode', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          item_id: item.item_id,
          query,
          allow_address_retry: false,
          conversation_id: state.currentRunId
        })
      });
      item.geometry = payload.geometry;
      item.geometry_status = payload.geometry.geometry_status;
      item.geometries = payload.geometry.geometries || [];
      item.area_m2 = payload.geometry.area_m2;
      item.geocode_query = query;
      if (payload.geometry.point) {
        setMarker(payload.geometry.point);
        state.geometryListTab = 'geocoded';
      } else {
        state.geometryListTab = 'manual';
        const assessment = (payload.spatial_validation.assessments || []).find(
          (candidate) => candidate.latitude != null && candidate.longitude != null
        );
        if (assessment && state.map) {
          state.map.setView([assessment.latitude, assessment.longitude], 14);
          $('coordinateDraftStatus').textContent =
            'A rejected candidate was used only to center the map; no coordinate was assigned.';
        }
      }
      renderGeometryList();
      $('resolutionReason').textContent = resolutionExplanation(item);
      renderCandidateOptions(item);
      if (state.sampleExtentVisible) renderSampleExtent(false);
      setGeometryStatus(
        payload.geocode_result
          ? 'Geocode placed an in-scope facility point.'
          : `Coordinate assignment requires human review: ${payload.spatial_validation.reason}`,
        payload.geocode_result ? 'ok' : ''
      );
      updateActionAvailability();
    }

    async function researchSelectedFacility() {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      state.pendingCoordinatePreview = null;
      const button = $('researchFacilityButton');
      button.disabled = true;
      setGeometryStatus(
        `Researching ${item.lead?.location?.facility_name || item.item_id}…`,
        'ok'
      );
      $('coordinateDraftStatus').textContent =
        'The address-spatial agent is researching official facility evidence.';
      try {
        const payload = await api('/api/geometry/research', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            item_id: item.item_id,
            conversation_id: state.currentRunId
          })
        });
        item.geometry = payload.geometry;
        item.geometry_status = payload.geometry.geometry_status;
        item.geometries = payload.geometry.geometries || [];
        item.area_m2 = payload.geometry.area_m2;
        item.geocode_query = payload.geometry.geocode_query;
        if (payload.address_retry?.address) {
          item.address_enrichment = payload.address_retry.address;
          item.address_status = payload.address_retry.address.status;
          $('manualAddress').value =
            payload.address_retry.address.formatted_address || item.geocode_query;
        }
        if (payload.research_resolved && payload.geometry.point) {
          state.geometryListTab = 'geocoded';
          setMarker(payload.geometry.point);
          $('coordinateDraftStatus').textContent =
            'Focused research produced and saved an in-scope coordinate.';
        } else {
          state.geometryListTab = 'manual';
          $('coordinateDraftStatus').textContent =
            'Focused research completed. Review the ranked candidates below.';
        }
        renderGeometryList();
        $('resolutionReason').textContent = resolutionExplanation(item);
        updateExternalSearchLinks(item);
        renderCandidateOptions(item);
        await loadDialogue();
        setGeometryStatus(
          payload.research_resolved
            ? 'Focused research resolved this coordinate.'
            : 'Focused research completed; candidate selection is ready.',
          'ok'
        );
      } finally {
        updateActionAvailability();
      }
    }

    async function acceptCandidate(index) {
      const item = selectedGeometryItem();
      const candidate = state.selectedCandidateOptions[index];
      if (!item || !candidate) {
        return setGeometryStatus('The selected candidate is no longer available.', 'error');
      }
      if (candidate.scope_status === 'out_of_scope') {
        return setGeometryStatus(
          'This candidate is outside the requested geographic scope and cannot be accepted.',
          'error'
        );
      }
      state.pendingCoordinatePreview = null;
      const point = {
        latitude: candidate.latitude,
        longitude: candidate.longitude,
        source: `${candidate.provider || 'geocoder'}-human`
      };
      setMarker(point);
      const spatialValidation = {
        ...(item.geometry?.spatial_validation || {}),
        status: 'human_accepted_candidate',
        requires_human_intervention: false,
        reason: 'A human reviewer accepted a ranked geocoder candidate.',
        accepted_candidate: candidate
      };
      await saveGeometry('point_confirmed', {
        point,
        geocodeResult: candidate.geocode_result || candidate,
        spatialValidation,
        reviewNotes:
          `Human accepted ${candidate.display_name || 'a ranked geocoder candidate'}.`
      });
    }

    async function geocodeAll() {
      if (!state.geometryItems.length) {
        setAutomatedGeocodeStatus('No accepted observations are available to geocode.', 'error');
        return null;
      }
      const alreadyPositioned = state.geometryItems.filter(
        (item) => pointFromGeometry(item)
      ).length;
      const pending = state.geometryItems.filter(
        (item) => !pointFromGeometry(item) && (item.geocode_query || '').trim()
      );
      const missingQuery = state.geometryItems.filter(
        (item) => !pointFromGeometry(item) && !(item.geocode_query || '').trim()
      ).length;
      if (!pending.length) {
        setAutomatedGeocodeStatus(
          `No observations need geocoding. ${alreadyPositioned} already have points` +
          `${missingQuery ? `; ${missingQuery} have no address query` : ''}.`,
          'ok'
        );
        return {
          geocoded: 0,
          attempted: 0,
          already_positioned: alreadyPositioned,
          missing_query: missingQuery,
          needs_human_review: 0,
          errors: 0
        };
      }
      const button = $('geocodeButton');
      button.disabled = true;
      let geocodedCount = 0;
      let notFoundCount = 0;
      let humanReviewCount = 0;
      let errorCount = 0;
      try {
        renderGeocodingProgress({
          attempted: 0,
          total: pending.length,
          geocoded: 0,
          humanReview: 0,
          errors: 0,
          working: true
        });
        for (let index = 0; index < pending.length; index += 1) {
          const item = pending[index];
          renderGeocodingProgress({
            attempted: index,
            total: pending.length,
            geocoded: geocodedCount,
            humanReview: humanReviewCount,
            errors: errorCount,
            working: true
          });
          setAutomatedGeocodeStatus(
            `Geocoding ${index + 1}/${pending.length}: ${item.geocode_query.trim()}`
          );
          try {
            const payload = await api('/api/geometry/geocode', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({
                item_id: item.item_id,
                query: item.geocode_query.trim(),
                allow_address_retry: true,
                conversation_id: state.currentRunId
              })
            });
            item.geometry = payload.geometry;
            item.geometry_status = payload.geometry.geometry_status;
            item.geometries = payload.geometry.geometries || [];
            item.area_m2 = payload.geometry.area_m2;
            item.geocode_query = payload.geometry.geocode_query;
            if (payload.address_retry?.address) {
              item.address_enrichment = payload.address_retry.address;
              item.address_status = payload.address_retry.address.status;
            }
            if (payload.geocode_result) geocodedCount += 1;
            else {
              notFoundCount += 1;
              if (payload.spatial_validation.requires_human_intervention) {
                humanReviewCount += 1;
              }
            }
          } catch (_) {
            errorCount += 1;
          }
          renderGeometryList();
          updateGeometrySummary();
          renderGeocodingProgress({
            attempted: index + 1,
            total: pending.length,
            geocoded: geocodedCount,
            humanReview: humanReviewCount,
            errors: errorCount,
            working: false
          });
        }
        const selected = selectedGeometryItem();
        if (
          state.activeWorkspace === 'geometry' &&
          selected &&
          pointFromGeometry(selected)
        ) {
          setMarker(pointFromGeometry(selected));
        }
        state.geometryListTab = manualGeometryItems().length ? 'manual' : 'geocoded';
        renderGeometryList();
        updateGeometrySummary();
        if (state.activeWorkspace === 'geometry' && state.sampleExtentVisible) {
          renderSampleExtent(false);
        }
        const suffix = [
          `${notFoundCount} not found`,
          `${humanReviewCount} need human coordinate assignment`,
          `${errorCount} error(s)`,
          `${alreadyPositioned} already positioned`,
          `${missingQuery} without an address query`
        ].join(', ');
        setAutomatedGeocodeStatus(
          `Geocoded ${geocodedCount} of ${pending.length} observation(s). ` +
          suffix + '.',
          errorCount ? 'error' : 'ok'
        );
        await loadWorkflowStatus();
        await loadDialogue();
        return {
          geocoded: geocodedCount,
          attempted: pending.length,
          already_positioned: alreadyPositioned,
          missing_query: missingQuery,
          needs_human_review: humanReviewCount,
          errors: errorCount
        };
      } finally {
        updateActionAvailability();
      }
    }

    async function searchManualAddress() {
      const query = $('manualAddress').value.trim();
      if (!query) return setGeometryStatus('Enter an address or place to search.', 'error');
      state.pendingCoordinatePreview = null;
      await geocodeSelected(query);
    }

    async function previewPastedCoordinates() {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const coordinateText = $('pastedCoordinates').value.trim();
      if (!coordinateText) {
        return setGeometryStatus('Paste coordinates or a Google Maps URL first.', 'error');
      }
      const button = $('previewCoordinatesButton');
      button.disabled = true;
      try {
        const payload = await api('/api/geometry/coordinate-preview', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            item_id: item.item_id,
            coordinate_text: coordinateText
          })
        });
        state.pendingCoordinatePreview = payload;
        setMarker(payload.point);
        const validation = payload.spatial_validation || {};
        const messages = [
          `Parsed coordinate: ${payload.normalized}.`,
          validation.reason || 'Geographic scope was not verified.'
        ];
        if (payload.reversed_order) {
          messages.push('Longitude and latitude appeared reversed and were corrected.');
        }
        $('coordinatePasteStatus').className =
          `status ${validation.warning ? 'error' : 'ok'}`;
        $('coordinatePasteStatus').textContent = messages.join(' ');
        $('coordinateDraftStatus').textContent = validation.warning
          ? 'Coordinate previewed with a warning. Verify the marker before saving.'
          : 'Coordinate previewed and passed the available geographic checks.';
        setGeometryStatus(
          validation.warning
            ? 'Coordinate previewed with a geographic warning.'
            : 'Coordinate previewed successfully. Select Save Coordinate to confirm.',
          validation.warning ? 'error' : 'ok'
        );
      } finally {
        updateActionAvailability();
      }
    }

    function useMapCenter() {
      const item = selectedGeometryItem();
      if (!item || !state.map) {
        return setGeometryStatus('No approved observation selected.', 'error');
      }
      const center = state.map.getCenter();
      state.pendingCoordinatePreview = null;
      setMarker({ latitude: center.lat, longitude: center.lng, source: 'user' });
      state.coordinatePlacementMode = false;
      $('map').classList.remove('placement-active');
      $('coordinateDraftStatus').textContent =
        'Draft coordinate placed at the map center. Select Save Coordinate to confirm it.';
      setGeometryStatus('Point set from map center.', 'ok');
      updateActionAvailability();
    }

    function startPointPlacement() {
      const item = selectedGeometryItem();
      if (!item || !state.map) {
        return setGeometryStatus('Select an observation before placing a point.', 'error');
      }
      state.pendingCoordinatePreview = null;
      state.coordinatePlacementMode = true;
      $('map').classList.add('placement-active');
      $('coordinateDraftStatus').textContent =
        'Placement mode active: click the facility location on the map.';
      setGeometryStatus('Click the facility location on the map.', 'ok');
      updateActionAvailability();
    }

    async function saveCoordinate() {
      if (!currentPointPayload()) {
        return setGeometryStatus(
          'Place a point on the map or search a corrected address before saving.',
          'error'
        );
      }
      if (state.pendingCoordinatePreview) {
        const preview = state.pendingCoordinatePreview;
        const item = selectedGeometryItem();
        const previewValidation = preview.spatial_validation || {};
        const savedPoint = currentPointPayload(preview.point.source);
        const markerMoved = Math.abs(savedPoint.latitude - preview.point.latitude) > 1e-7 ||
          Math.abs(savedPoint.longitude - preview.point.longitude) > 1e-7;
        const savedNormalized =
          `${savedPoint.latitude.toFixed(7)}, ${savedPoint.longitude.toFixed(7)}`;
        await saveGeometry('point_confirmed', {
          point: savedPoint,
          geocodeResult:
            preview.reverse_geocode_result || item?.geometry?.geocode_result || null,
          spatialValidation: {
            ...(item?.geometry?.spatial_validation || {}),
            status: 'human_pasted_coordinate',
            requires_human_intervention: false,
            reason: previewValidation.warning
              ? (
                'A human reviewer saved a pasted Google Maps coordinate after reviewing ' +
                `this warning: ${previewValidation.reason}`
              )
              : (
                'A human reviewer saved a pasted Google Maps coordinate after preview.' +
                (markerMoved ? ' The reviewer adjusted the marker after preview.' : '')
              ),
            pasted_coordinate_validation: previewValidation,
            pasted_coordinate_text: $('pastedCoordinates').value.trim(),
            normalized_coordinate: savedNormalized,
            marker_adjusted_after_preview: markerMoved
          },
          reviewNotes: $('coordinateReviewNotes').value.trim() || (
            `Human-reviewed Google Maps coordinate: ${savedNormalized}.`
          )
        });
        state.pendingCoordinatePreview = null;
        $('coordinatePasteStatus').className = 'status ok';
        $('coordinatePasteStatus').textContent =
          `Saved Google Maps coordinate ${savedNormalized}.`;
        return;
      }
      await saveGeometry('point_confirmed');
    }

    async function saveFootprint() {
      if (!currentPolygonGeojson()) {
        return setGeometryStatus(
          'Draw a building polygon on the map before saving a footprint.',
          'error'
        );
      }
      await saveGeometry('footprint_drawn');
    }

    async function saveGeometry(status = null, overrides = {}) {
      const item = selectedGeometryItem();
      if (!item) return setGeometryStatus('No approved observation selected.', 'error');
      const polygon = currentPolygonGeojson();
      const point = overrides.point || currentPointPayload();
      const geometryStatus = status || (polygon ? 'footprint_drawn' : 'point_confirmed');
      const payload = await api(`/api/geometry/items/${item.item_id}`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          item_id: item.item_id,
          geocode_query: item.geocode_query,
          point,
          polygon_geojson: polygon,
          geometry_status: geometryStatus,
          geocode_result: overrides.geocodeResult ?? item.geometry?.geocode_result ?? null,
          spatial_validation:
            overrides.spatialValidation ?? item.geometry?.spatial_validation ?? null,
          review_notes: overrides.reviewNotes || (
            geometryStatus === 'skipped'
              ? 'Skipped in geometry review.'
              : ($('coordinateReviewNotes').value.trim() || null)
          ),
          conversation_id: state.currentRunId
        })
      });
      item.geometry = payload.geometry;
      item.geometry_status = payload.geometry.geometry_status;
      item.geometries = payload.geometry.geometries || [];
      item.area_m2 = payload.geometry.area_m2;
      if (geometryStatus === 'point_confirmed' && pointFromGeometry(item)) {
        state.geometryListTab = 'geocoded';
      } else if (needsManualGeocoding(item)) {
        state.geometryListTab = 'manual';
      }
      renderGeometryList();
      $('resolutionReason').textContent = resolutionExplanation(item);
      renderCandidateOptions(item);
      $('coordinateDraftStatus').textContent =
        geometryStatus === 'point_confirmed'
          ? 'Coordinate saved and removed from the intervention queue.'
          : 'Geometry saved.';
      $('jsonOutput').value = JSON.stringify(state.geometryItems, null, 2);
      if (state.sampleExtentVisible) renderSampleExtent(false);
      selectGeometryItem(item.item_id);
      const areaMessage = item.area_m2 == null
        ? ''
        : ` Area: ${Math.round(item.area_m2).toLocaleString()} square meters.`;
      setGeometryStatus(`Geometry saved: ${item.geometry_status}.${areaMessage}`, 'ok');
      await loadWorkflowStatus();
      await loadDialogue();
      updateActionAvailability();
    }

    async function exitApplication() {
      if (!window.confirm('Exit OASIS and cancel active harvests?')) return;
      try {
        await api('/api/app/exit', { method: 'POST' });
      } catch (_) {
        // The server may close before the browser receives the response.
      }
      stopPolling();
      setStatus('Server shutting down.', 'ok');
      $('activityOutput').value += '\\nServer shutting down. You may close this tab.\\n';
    }

    function downloadText(filename, text, type) {
      const blob = new Blob([text], { type });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    }

    async function downloadExport(format) {
      if (!state.currentRunId) return setStatus('No run selected.', 'error');
      const response = await fetch(`/api/runs/${state.currentRunId}/export.verified.${format}`);
      if (!response.ok) return setStatus(await response.text(), 'error');
      downloadText(
        `observation-harvest.verified.${format}`,
        await response.text(),
        format === 'csv' ? 'text/csv' : 'application/json'
      );
    }

    async function downloadFootprints() {
      if (!state.currentRunId) return setGeometryStatus('No run selected.', 'error');
      const response = await fetch(`/api/runs/${state.currentRunId}/export.footprints.geojson`);
      if (!response.ok) return setGeometryStatus(await response.text(), 'error');
      downloadText(
        'observation-footprints.geojson',
        await response.text(),
        'application/geo+json'
      );
    }

    async function downloadSampleExport(format) {
      if (!state.currentSampleSetId) {
        return setSampleStatus('No review dataset selected.', 'error');
      }
      const response = await fetch(
        `/api/samples/${state.currentSampleSetId}/export.verified.${format}`
      );
      if (!response.ok) return setSampleStatus(await response.text(), 'error');
      downloadText(
        `observation-sample.verified.${format}`,
        await response.text(),
        format === 'csv' ? 'text/csv' : 'application/json'
      );
    }

    async function downloadSampleFootprints() {
      if (!state.currentSampleSetId) {
        return setGeometryStatus('No review dataset selected.', 'error');
      }
      const response = await fetch(
        `/api/samples/${state.currentSampleSetId}/export.footprints.geojson`
      );
      if (!response.ok) return setGeometryStatus(await response.text(), 'error');
      downloadText(
        'observation-sample-footprints.geojson',
        await response.text(),
        'application/geo+json'
      );
    }

    function bindTabKeyboard(tabEntries, activate) {
      const ids = tabEntries.map(([id]) => id);
      for (const [id, value] of tabEntries) {
        $(id).addEventListener('keydown', (event) => {
          if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
          event.preventDefault();
          const currentIndex = ids.indexOf(id);
          let nextIndex = currentIndex;
          if (event.key === 'Home') nextIndex = 0;
          else if (event.key === 'End') nextIndex = ids.length - 1;
          else if (event.key === 'ArrowLeft') nextIndex = (currentIndex - 1 + ids.length) % ids.length;
          else if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % ids.length;
          const [nextId, nextValue] = tabEntries[nextIndex];
          $(nextId).focus();
          activate(nextValue);
        });
      }
    }

    async function boot() {
      initTheme();
      setWorkspaceTab('workbench');
      renderWorkflow(null);
      const payload = await api('/api/profiles');
      state.profiles = payload.profile_sets;
      renderProfileSets();
      await loadRuns();
      $('themeSelect').addEventListener('change', () => {
        localStorage.setItem('observationHarvesterTheme', $('themeSelect').value);
        applyTheme($('themeSelect').value);
      });
      $('workbenchTab').addEventListener('click', () => setWorkspaceTab('workbench'));
      $('geometryTab').addEventListener('click', () => setWorkspaceTab('geometry'));
      $('tableTab').addEventListener('click', () => setWorkspaceTab('table'));
      bindTabKeyboard(workspaceTabs, setWorkspaceTab);
      $('profileSet').addEventListener('change', renderProfiles);
      $('singleMode').addEventListener('click', () => setMode('single'));
      $('batchMode').addEventListener('click', () => setMode('batch'));
      $('campaignMode').addEventListener('click', () => setMode('campaign'));
      $('runFullPipelineButton').addEventListener('click', runFullPipeline);
      $('runButton').addEventListener('click', runHarvest);
      $('refreshButton').addEventListener('click', loadRuns);
      $('clearRunsButton').addEventListener('click', () => {
        clearRuns().catch((error) => setStatus(error.message, 'error'));
      });
      $('cancelButton').addEventListener('click', cancelRun);
      $('exitButton').addEventListener('click', exitApplication);
      $('copyButton').addEventListener('click', async () => {
        await navigator.clipboard.writeText($('jsonOutput').value);
        setStatus('JSON copied.', 'ok');
      });
      $('copyQaqcButton').addEventListener('click', copyQaqcPrompt);
      $('downloadTranscriptButton').addEventListener('click', () => {
        downloadTranscript().catch((error) => setStatus(error.message, 'error'));
      });
      $('runQaqcButton').addEventListener('click', runQaqc);
      $('runAddressButton').addEventListener('click', runAddressEnrichment);
      $('workflowNextButton').addEventListener('click', () => {
        workflowAction($('workflowNextButton').dataset.action);
      });
      $('workflowSteps').addEventListener('click', (event) => {
        const button = event.target.closest('[data-workflow-action]');
        if (button) workflowAction(button.dataset.workflowAction);
      });
      $('createSampleButton').addEventListener('click', () => {
        createSampleSet().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('analyzeCoverageButton').addEventListener('click', () => {
        analyzeCoverage().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('runGapFillButton').addEventListener('click', () => {
        runGapFill().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('runSampleQaqcButton').addEventListener('click', () => {
        runSampleQaqcMissing().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('runSampleAddressButton').addEventListener('click', () => {
        runSampleAddressMissing().catch((error) => setSampleStatus(error.message, 'error'));
      });
      $('downloadSampleJsonButton').addEventListener('click', () => downloadSampleExport('json'));
      $('downloadSampleCsvButton').addEventListener('click', () => downloadSampleExport('csv'));
      $('downloadJsonButton').addEventListener('click', () => downloadExport('json'));
      $('downloadCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('tableVerifiedMode').addEventListener('click', () => setTableMode('verified'));
      $('tableAllMode').addEventListener('click', () => setTableMode('all'));
      $('curationIncludedFilter').addEventListener('click', () => setCurationFilter('included'));
      $('curationExcludedFilter').addEventListener('click', () => setCurationFilter('excluded'));
      $('curationAllFilter').addEventListener('click', () => setCurationFilter('all'));
      $('selectVisibleButton').addEventListener('click', () => {
        const visibleIds = new Set(
          state.tableVisibleRows.map((row) => row.item_id).filter(Boolean)
        );
        const allSelected = Array.from(visibleIds).every((itemId) =>
          state.selectedCurationItemIds.has(itemId)
        );
        for (const itemId of visibleIds) {
          if (allSelected) state.selectedCurationItemIds.delete(itemId);
          else state.selectedCurationItemIds.add(itemId);
        }
        renderDataTable();
      });
      $('excludeSelectedButton').addEventListener('click', () => {
        excludeSelectedObservations().catch((error) => setTableStatus(error.message, 'error'));
      });
      $('restoreSelectedButton').addEventListener('click', () => {
        restoreSelectedObservations().catch((error) => setTableStatus(error.message, 'error'));
      });
      $('approveCurationButton').addEventListener('click', () => {
        approveCurationAndAnalyzeCoverage().catch(
          (error) => setTableStatus(error.message, 'error')
        );
      });
      $('tableSearch').addEventListener('input', () => {
        renderDataTable();
        updateActionAvailability();
      });
      $('tableClearSearchButton').addEventListener('click', () => {
        $('tableSearch').value = '';
        renderDataTable();
      });
      $('tableRefreshButton').addEventListener('click', () => {
        refreshDataTable().catch((error) => setTableStatus(error.message, 'error'));
      });
      $('tableCopyButton').addEventListener('click', () => {
        copyVisibleTableRows().catch((error) => setTableStatus(error.message, 'error'));
      });
      $('tableCsvButton').addEventListener('click', downloadVisibleTableCsv);
      $('tableHead').addEventListener('click', (event) => {
        const button = event.target.closest('[data-table-sort]');
        if (!button) return;
        const key = button.dataset.tableSort;
        if (state.tableSortKey === key) {
          state.tableSortDirection = state.tableSortDirection === 'asc' ? 'desc' : 'asc';
        } else {
          state.tableSortKey = key;
          state.tableSortDirection = 'asc';
        }
        renderDataTable();
      });
      $('tableBody').addEventListener('click', (event) => {
        const button = event.target.closest('[data-table-open-geometry]');
        if (!button) return;
        openTableRowInGeometry(button.dataset.tableOpenGeometry).catch(
          (error) => setTableStatus(error.message, 'error')
        );
      });
      $('tableBody').addEventListener('change', (event) => {
        const checkbox = event.target.closest('[data-curation-item]');
        if (!checkbox) return;
        const itemId = checkbox.dataset.curationItem;
        if (checkbox.checked) state.selectedCurationItemIds.add(itemId);
        else state.selectedCurationItemIds.delete(itemId);
        renderCurationSummary();
        updateActionAvailability();
      });
      $('loadApprovedButton').addEventListener('click', () => {
        loadApprovedGeometry().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('loadAugmentedSampleButton').addEventListener('click', () => {
        loadAugmentedSampleGeometry().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('geocodedQueueTab').addEventListener('click', () => setGeometryListTab('geocoded'));
      $('manualQueueTab').addEventListener('click', () => setGeometryListTab('manual'));
      $('geocodeButton').addEventListener('click', () => {
        geocodeAcceptedObservations().catch(
          (error) => setAutomatedGeocodeStatus(error.message, 'error')
        );
      });
      $('searchAddressButton').addEventListener('click', () => {
        searchManualAddress().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('researchFacilityButton').addEventListener('click', () => {
        researchSelectedFacility().catch(
          (error) => setGeometryStatus(error.message, 'error')
        );
      });
      $('previewCoordinatesButton').addEventListener('click', () => {
        previewPastedCoordinates().catch(
          (error) => setGeometryStatus(error.message, 'error')
        );
      });
      $('placePointButton').addEventListener('click', startPointPlacement);
      $('useMapCenterButton').addEventListener('click', useMapCenter);
      $('saveCoordinateButton').addEventListener('click', () => {
        saveCoordinate().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('saveFootprintButton').addEventListener('click', () => {
        saveFootprint().catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('skipGeometryButton').addEventListener('click', () => {
        saveGeometry('skipped').catch((error) => setGeometryStatus(error.message, 'error'));
      });
      $('showSampleExtentButton').addEventListener('click', () => renderSampleExtent(true));
      $('zoomSampleExtentButton').addEventListener('click', zoomSampleExtent);
      $('clearSampleExtentButton').addEventListener('click', clearSampleExtent);
      $('downloadVerifiedJsonButton').addEventListener('click', () => downloadExport('json'));
      $('downloadVerifiedCsvButton').addEventListener('click', () => downloadExport('csv'));
      $('downloadFootprintsButton').addEventListener('click', downloadFootprints);
      $('downloadSampleFootprintsButton').addEventListener('click', downloadSampleFootprints);
    }
    boot().catch((error) => setStatus(error.message, 'error'));
