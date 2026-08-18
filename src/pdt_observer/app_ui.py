INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OASIS</title>
  <link rel="icon" href="/assets/oasis-logo.jpg" type="image/jpeg">
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
  >
  <link
    rel="stylesheet"
    href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"
  >
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
  <link rel="stylesheet" href="/assets/app.css">
</head>
<body>
  <header>
    <div class="brand">
      <span class="brand-mark">
        <img
          class="brand-logo"
          src="/assets/oasis-logo.jpg"
          alt=""
          aria-hidden="true"
        >
      </span>
      <div>
        <h1>OASIS</h1>
        <div class="workflow-summary">
          Observation Acquisition and Spatial Information Synthesis
        </div>
      </div>
    </div>
    <div class="header-actions">
      <div class="theme-control">
        <label for="themeSelect">Theme</label>
        <select id="themeSelect">
          <option value="system">System</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
      </div>
      <button
        id="exitButton"
        class="secondary"
        type="button"
        title="Stops the local OASIS server and closes this tab when the browser allows it."
      >
        Exit OASIS
      </button>
    </div>
  </header>
  <nav class="workspace-tabs" role="tablist" aria-label="Application workspaces">
    <button
      id="workbenchTab"
      class="active"
      type="button"
      role="tab"
      aria-selected="true"
      aria-pressed="true"
      aria-controls="harvestSetup resultsPanel samplePanel"
    >
      Agentic Workbench
    </button>
    <button
      id="geometryTab"
      type="button"
      role="tab"
      aria-selected="false"
      aria-pressed="false"
      aria-controls="geometryPanel"
    >
      Geometry Studio <span id="geometryTabBadge" class="tab-badge">0</span>
    </button>
    <button
      id="tableTab"
      type="button"
      role="tab"
      aria-selected="false"
      aria-pressed="false"
      aria-controls="dataTablePanel"
    >
      Tabular Data <span id="tableTabBadge" class="tab-badge">0</span>
    </button>
  </nav>
  <main>
    <section
      id="harvestSetup"
      data-workspace="workbench"
      role="tabpanel"
      aria-labelledby="workbenchTab"
    >
      <h2>Setup Harvest</h2>
      <div class="friendly-empty">
        Start with <strong>Run Full Pipeline</strong> for the guided pilot workflow. Manual
        harvest-only controls are available in advanced options.
      </div>
      <label for="country">Country</label>
      <input id="country" value="US" autocomplete="off">

      <div id="singleLocalityBlock">
        <label for="locality">Region or Locality</label>
        <input id="locality" placeholder="Optional, e.g. Tennessee">
      </div>

      <div id="campaignLocalitiesBlock" class="hidden">
        <label for="localities">Regions or Localities</label>
        <textarea
          id="localities"
          class="compact"
          spellcheck="false"
          placeholder="Optional, one per line"
        ></textarea>
      </div>

      <div id="singleFacilityBlock">
        <label for="profileSet">Land Use</label>
        <select id="profileSet"></select>
      </div>

      <div id="campaignFacilityBlock" class="hidden">
        <label for="campaignFacilityTypes">Land Uses</label>
        <select id="campaignFacilityTypes" multiple></select>
      </div>

      <div id="subtypeBlock">
        <label for="profile">Facility Class</label>
        <select id="profile"></select>
      </div>

      <div>
        <label for="countMethod">Count Mode</label>
        <select id="countMethod">
          <option value="">Profile default</option>
          <option value="direct_count">Direct count</option>
          <option value="population_subcomponent">Subcomponent count</option>
        </select>
        <p class="field-help">
          To compare direct occupancy with subcomponent bundles, run the two modes separately.
        </p>
      </div>

      <div class="row">
        <div>
          <label for="target">Target</label>
          <input id="target" type="number" min="1" value="20">
        </div>
        <div>
          <label>Mode</label>
          <div class="mode" role="group" aria-label="Harvest mode">
            <button id="singleMode" class="active" type="button" aria-pressed="true">Single</button>
            <button id="batchMode" type="button" aria-pressed="false">Batch</button>
            <button id="campaignMode" type="button" aria-pressed="false">Campaign</button>
          </div>
        </div>
      </div>

      <div class="actions">
        <button id="runFullPipelineButton" type="button">Run Full Pipeline</button>
        <button id="refreshButton" class="secondary" type="button">Refresh Runs</button>
        <button id="clearRunsButton" class="secondary" type="button">Clear Generated Runs</button>
      </div>
      <div id="setupActionHelp" class="control-help">
        Guided mode will pause before coverage approval so you can review the dataset.
      </div>
      <div class="pipeline-callout">
        <strong id="fullPipelineHeading">Guided end-to-end workflow</strong>
        <span id="fullPipelineStatus" role="status" aria-live="polite">
          Runs through review dataset assembly, then pauses for optional exclusions and approval.
        </span>
      </div>
      <details class="action-group">
        <summary>Advanced manual stages</summary>
        <div class="actions">
          <button id="runButton" class="secondary" type="button">Run Harvest Only</button>
        </div>
        <div class="control-help">
          Use harvest-only when you want to run discovery first, then manually trigger QAQC,
          address enrichment, geometry review, and coverage steps.
        </div>
      </details>
      <div id="status" class="status" role="status" aria-live="polite">Ready.</div>
      <div class="history" id="history"></div>
    </section>

    <section
      id="resultsPanel"
      data-workspace="workbench"
      role="tabpanel"
      aria-labelledby="workbenchTab"
    >
      <h2>Results</h2>
      <div class="workflow-panel">
        <div class="workflow-header">
          <div>
            <h3>Project Workflow</h3>
            <div id="workflowSummary" class="workflow-summary">
              Start or select a harvest to see the full workflow.
            </div>
          </div>
          <button id="workflowNextButton" type="button">
            Next action
          </button>
        </div>
        <div id="workflowSteps" class="workflow-list"></div>
      </div>
      <div class="summary">
        <div class="metric"><span>Status</span><strong id="metricStatus">-</strong></div>
        <div class="metric"><span>Leads</span><strong id="metricLeads">0</strong></div>
        <div class="metric">
          <span id="metricFacilityLabel">Facility</span><strong id="metricFacility">0</strong>
        </div>
        <div class="metric">
          <span id="metricAggregateLabel">Aggregate</span><strong id="metricAggregate">0</strong>
        </div>
      </div>
      <div class="actions">
        <button id="runQaqcButton" class="secondary" type="button" disabled>Run QAQC</button>
        <button id="runAddressButton" class="secondary" type="button" disabled>
          Run Address Enrichment
        </button>
        <button id="geocodeButton" class="secondary" type="button" disabled>
          Geocode QAQC-Approved Observations
        </button>
      </div>
      <div id="resultsActionHelp" class="control-help">
        Start or select a completed harvest to unlock QAQC, address enrichment, and geocoding.
      </div>
      <details class="action-group">
        <summary>Raw outputs and prompts</summary>
        <div class="actions">
          <button id="copyButton" class="secondary" type="button" disabled>Copy JSON</button>
          <button id="copyQaqcButton" class="secondary" type="button" disabled>
            Copy QAQC Prompt
          </button>
        </div>
        <textarea
          id="jsonOutput"
          class="output-panel"
          spellcheck="false"
          placeholder="Harvest JSON will appear here."
        ></textarea>
      </details>
      <details class="action-group">
        <summary>Exports</summary>
        <div class="actions">
          <button id="downloadJsonButton" class="secondary" type="button" disabled>
            Download Verified JSON
          </button>
          <button id="downloadCsvButton" class="secondary" type="button" disabled>
            Download Verified CSV
          </button>
        </div>
        <div id="exportActionHelp" class="control-help">
          Verified exports unlock after QAQC has produced keep decisions.
        </div>
      </details>

      <details class="action-group">
        <summary>Agent transcript and activity</summary>
        <div class="section-heading">
          <h2>Full Pipeline Transcript</h2>
          <button id="downloadTranscriptButton" class="secondary" type="button" disabled>
            Download Transcript (.txt)
          </button>
        </div>
        <textarea
          id="dialogueOutput"
          class="dialogue output-panel"
          spellcheck="false"
          readonly
          aria-label="Full pipeline transcript"
          placeholder="Pipeline transcript will appear here."
        ></textarea>

        <h2>Agent Activity</h2>
        <div class="actions">
          <button id="cancelButton" class="secondary" type="button" disabled>Cancel Run</button>
        </div>
        <textarea
          id="activityOutput"
          class="activity output-panel"
          spellcheck="false"
          readonly
          aria-label="Agent activity log"
          placeholder="Agent activity will appear here while a harvest runs."
        ></textarea>
      </details>
    </section>

    <section
      id="geometryPanel"
      class="wide hidden"
      data-workspace="geometry"
      role="tabpanel"
      aria-labelledby="geometryTab"
    >
      <h2>Geometry Studio</h2>
      <div class="workflow-summary">
        Resolve coordinates, inspect spatial placement, digitize building footprints, and
        calculate planar area.
      </div>
      <div class="friendly-empty">
        Load approved observations after QAQC and address enrichment, then resolve only the
        locations that need human judgment.
      </div>
      <div class="actions">
        <button id="loadApprovedButton" class="secondary" type="button" disabled>
          Load QAQC-Approved Observations
        </button>
        <button id="loadAugmentedSampleButton" class="secondary" type="button" disabled>
          Load Review Dataset Observations
        </button>
      </div>
      <div id="geometryLoadHelp" class="control-help">
        Select a run or review dataset before loading geometry items.
      </div>
      <div class="actions">
        <button id="saveFootprintButton" class="secondary" type="button" disabled>
          Save Footprint
        </button>
        <button id="skipGeometryButton" class="secondary" type="button" disabled>Skip</button>
      </div>
      <div id="geometryActionHelp" class="control-help">
        Select an observation before saving a footprint or skipping geometry review.
      </div>
      <details class="action-group">
        <summary>Map view and exports</summary>
        <div class="actions">
          <button id="showSampleExtentButton" class="secondary" type="button" disabled>
            Show Sample Extent
          </button>
          <button id="zoomSampleExtentButton" class="secondary" type="button" disabled>
            Zoom To Extent
          </button>
          <button id="clearSampleExtentButton" class="secondary" type="button" disabled>
            Clear Extent
          </button>
          <button id="downloadVerifiedJsonButton" class="secondary" type="button" disabled>
            Download Verified JSON
          </button>
          <button id="downloadVerifiedCsvButton" class="secondary" type="button" disabled>
            Download Verified CSV
          </button>
          <button id="downloadAdminJsonButton" class="secondary" type="button" disabled>
            Download Admin-Scoped JSON
          </button>
          <button id="downloadAdminCsvButton" class="secondary" type="button" disabled>
            Download Admin-Scoped CSV
          </button>
          <button id="downloadFootprintsButton" class="secondary" type="button" disabled>
            Download Footprints GeoJSON
          </button>
          <button id="downloadSampleAdminJsonButton" class="secondary" type="button" disabled>
            Download Sample Admin-Scoped JSON
          </button>
          <button id="downloadSampleAdminCsvButton" class="secondary" type="button" disabled>
            Download Sample Admin-Scoped CSV
          </button>
          <button id="downloadSampleFootprintsButton" class="secondary" type="button" disabled>
            Download Sample Footprints
          </button>
        </div>
      </details>
      <div class="extent-summary" id="geometryExtentSummary">
        Extent: load approved observations, then geocode or save points to map the sample.
      </div>
      <div id="geometryStatus" class="status" role="status" aria-live="polite">
        Load approved observations after QAQC and address enrichment.
      </div>
      <div class="intervention-panel">
        <h3>Coordinate Assignment Required - <span id="interventionCount">0</span></h3>
        <div class="workflow-summary">
          These observations could not be assigned a trustworthy in-scope facility coordinate.
          Select one, search a better address, or place its point from the map center.
        </div>
        <div id="interventionList" class="intervention-list">
          <span class="workflow-summary">No observations currently require intervention.</span>
        </div>
      </div>
      <div class="geometry-layout">
        <div>
          <div class="geometry-queue-tabs" role="tablist" aria-label="Geometry observation queues">
            <button
              id="geocodedQueueTab"
              class="active"
              type="button"
              role="tab"
              aria-selected="true"
              aria-pressed="true"
            >
              Geocoded <span id="geocodedQueueCount">0</span>
            </button>
            <button
              id="manualQueueTab"
              type="button"
              role="tab"
              aria-selected="false"
              aria-pressed="false"
            >
              Needs Manual Geocoding <span id="manualQueueCount">0</span>
            </button>
          </div>
          <div class="geometry-list" id="geometryList"></div>
          <div class="coordinate-resolver">
            <h3>Resolve Selected Coordinate</h3>
            <div id="resolutionReason" class="resolution-reason">
              Select an observation to see why automatic coordinate assignment failed.
            </div>
            <div class="resolution-links">
              <a id="resolutionSourceLink" class="hidden" target="_blank" rel="noopener">
                Open occupancy source
              </a>
              <a id="resolutionAddressLink" class="hidden" target="_blank" rel="noopener">
                Open address evidence
              </a>
              <a id="googleSearchLink" class="hidden" target="_blank" rel="noopener">
                Search Google
              </a>
              <a id="googleMapsLink" class="hidden" target="_blank" rel="noopener">
                Search Google Maps
              </a>
            </div>
            <div class="actions">
              <button id="researchFacilityButton" class="secondary" type="button" disabled>
                Research This Facility
              </button>
            </div>
            <div id="candidateOptions" class="candidate-options">
              <span class="workflow-summary">
                Ranked geocoder candidates will appear here after an automatic search.
              </span>
            </div>
            <label for="pastedCoordinates">Paste Google Maps Coordinates</label>
            <input
              id="pastedCoordinates"
              placeholder="33.7490, -84.3880 or paste a Google Maps URL"
            >
            <div class="actions">
              <button id="previewCoordinatesButton" class="secondary" type="button" disabled>
                Preview Coordinate
              </button>
            </div>
            <div id="coordinatePasteStatus" class="status" role="status" aria-live="polite">
              Preview pasted coordinates before saving them.
            </div>
            <label for="manualAddress">Corrected Address or Place</label>
            <input id="manualAddress" placeholder="Enter a corrected facility address">
            <div class="actions">
              <button id="searchAddressButton" class="secondary" type="button" disabled>
                Search Corrected Address
              </button>
              <button id="placePointButton" class="secondary" type="button" disabled>
                Place Point on Map
              </button>
              <button id="useMapCenterButton" class="secondary" type="button" disabled>
                Place at Map Center
              </button>
              <button id="saveCoordinateButton" type="button" disabled>Save Coordinate</button>
            </div>
            <label for="coordinateReviewNotes">Coordinate Review Notes</label>
            <input
              id="coordinateReviewNotes"
              placeholder="Optional evidence or reasoning for the manual assignment"
            >
            <div id="coordinateDraftStatus" class="status" role="status" aria-live="polite">
              No coordinate change is waiting to be saved.
            </div>
            <div id="geometryResolverHelp" class="control-help">
              Select an observation to unlock coordinate search, placement, and save actions.
            </div>
          </div>
          <label for="geometryDetail">Selected Observation</label>
          <textarea
            id="geometryDetail"
            class="compact"
            spellcheck="false"
            readonly
          ></textarea>
        </div>
        <div id="map" class="map"></div>
      </div>
    </section>

    <section
      id="dataTablePanel"
      class="wide hidden"
      data-workspace="table"
      role="tabpanel"
      aria-labelledby="tableTab"
    >
      <h2>Tabular Data</h2>
      <div class="workflow-summary" id="tableContext">
        Select or assemble a review dataset to inspect collected observations as rows.
      </div>
      <div class="friendly-empty">
        This workspace becomes most useful after QAQC or review-dataset assembly. Use it to
        search, curate, copy, and export visible rows.
      </div>
      <div class="table-toolbar">
        <div>
          <label>Rows</label>
          <div class="table-mode" role="group" aria-label="Table row mode">
            <button
              id="tableVerifiedMode"
              class="active"
              type="button"
              aria-pressed="true"
              disabled
            >
              Verified Only
            </button>
            <button id="tableAllMode" type="button" aria-pressed="false" disabled>All Leads</button>
          </div>
        </div>
        <div>
          <label for="tableSearch">Search</label>
          <input id="tableSearch" placeholder="Filter visible rows" disabled>
        </div>
        <button id="tableClearSearchButton" class="secondary" type="button" disabled>
          Clear Search
        </button>
        <button id="tableRefreshButton" class="secondary" type="button" disabled>
          Refresh Table
        </button>
        <button id="tableCopyButton" class="secondary" type="button" disabled>
          Copy Visible Rows
        </button>
        <button id="tableCsvButton" class="secondary" type="button" disabled>Download CSV</button>
      </div>
      <div id="tableActionHelp" class="control-help">
        Select a run or review dataset to load table rows.
      </div>
      <div id="curationPanel" class="curation-panel hidden">
        <div class="workflow-header">
          <div>
            <h3>Human Curation</h3>
            <div id="curationSummary" class="workflow-summary">
              Approve all observations, or select only those that should be excluded.
            </div>
          </div>
          <button id="approveCurationButton" type="button" disabled>
            Approve Dataset &amp; Check Coverage
          </button>
        </div>
        <div class="curation-controls">
          <div>
            <label>Show</label>
            <div class="curation-filter" role="group" aria-label="Curation filter">
              <button
                id="curationIncludedFilter"
                class="secondary active"
                type="button"
                aria-pressed="true"
              >
                Included
              </button>
              <button
                id="curationExcludedFilter"
                class="secondary"
                type="button"
                aria-pressed="false"
              >
                Excluded
              </button>
              <button id="curationAllFilter" class="secondary" type="button" aria-pressed="false">
                All
              </button>
            </div>
          </div>
          <button id="selectVisibleButton" class="secondary" type="button" disabled>
            Select Visible
          </button>
          <div>
            <label for="exclusionReason">Exclusion reason</label>
            <select id="exclusionReason">
              <option value="wrong_facility">Wrong facility or observation type</option>
              <option value="duplicate">Duplicate</option>
              <option value="outside_geographic_scope">Outside geographic scope</option>
              <option value="evidence_insufficient">Evidence insufficient</option>
              <option value="incorrect_count_meaning">Count meaning is incorrect</option>
              <option value="unrepresentative">Unrepresentative observation</option>
              <option value="address_or_coordinate_unresolved">Location unresolved</option>
              <option value="facility_type_not_relevant">Facility type not relevant</option>
              <option value="other">Other (note required)</option>
            </select>
          </div>
          <div>
            <label for="exclusionNote">Reasoning (optional)</label>
            <input id="exclusionNote" placeholder="Brief context for the coverage agent">
          </div>
          <button id="excludeSelectedButton" class="secondary" type="button" disabled>
            Exclude Selected
          </button>
          <button id="restoreSelectedButton" class="secondary" type="button" disabled>
            Restore Selected
          </button>
        </div>
        <div id="curationStatus" class="status" role="status" aria-live="polite">
          No individual review is required. Approval with no exclusions is valid.
        </div>
        <div id="curationActionHelp" class="control-help">
          Select rows only when you want to exclude or restore them; approval with no
          exclusions is valid.
        </div>
      </div>
      <div class="summary">
        <div class="metric"><span>Context</span><strong id="tableMetricContext">-</strong></div>
        <div class="metric"><span>Mode</span><strong id="tableMetricMode">Verified</strong></div>
        <div class="metric"><span>Rows</span><strong id="tableMetricRows">0</strong></div>
        <div class="metric"><span>Visible</span><strong id="tableMetricVisible">0</strong></div>
      </div>
      <div id="tableStatus" class="status" role="status" aria-live="polite">Ready.</div>
      <div id="tableEmpty" class="table-empty">
        No tabular data loaded.
      </div>
      <div id="tableWrap" class="data-table-wrap hidden">
        <table class="data-table">
          <thead id="tableHead"></thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </section>

    <section
      id="samplePanel"
      class="wide"
      data-workspace="workbench"
      role="tabpanel"
      aria-labelledby="workbenchTab"
    >
      <h2>Review Dataset / Coverage</h2>
      <div class="actions">
        <button id="createSampleButton" class="secondary" type="button" disabled>
          Assemble Review Dataset
        </button>
        <button id="analyzeCoverageButton" class="secondary" type="button" disabled>
          Check Coverage
        </button>
        <button id="runGapFillButton" class="secondary" type="button" disabled>
          Run Targeted Follow-ups
        </button>
      </div>
      <div id="sampleActionHelp" class="control-help">
        Assemble a review dataset from a selected run before coverage or follow-up work.
      </div>
      <details class="action-group">
        <summary>Fix Missing Pipeline Stages</summary>
        <div class="actions">
          <button id="runSampleQaqcButton" class="secondary" type="button" disabled>
            Run QAQC Missing
          </button>
          <button id="runSampleAddressButton" class="secondary" type="button" disabled>
            Run Address Missing
          </button>
          <button id="downloadSampleJsonButton" class="secondary" type="button" disabled>
            Download Sample JSON
          </button>
          <button id="downloadSampleCsvButton" class="secondary" type="button" disabled>
            Download Sample CSV
          </button>
        </div>
        <div id="repairActionHelp" class="control-help">
          Missing-stage fixes and sample exports unlock after review-dataset assembly.
        </div>
      </details>
      <div class="status" id="sampleStatus" role="status" aria-live="polite">
        Assemble a review dataset after geometry review; coverage works best once approved
        observations have geocoded points.
      </div>
      <textarea
        id="sampleOutput"
        class="compact output-panel"
        spellcheck="false"
        readonly
        aria-label="Review dataset and coverage output"
        placeholder="Review dataset and coverage output will appear here."
      ></textarea>
    </section>
  </main>
  <script src="/assets/app.js"></script>
</body>
</html>
"""
