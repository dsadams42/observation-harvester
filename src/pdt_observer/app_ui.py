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
    <div class="theme-control">
      <label for="themeSelect">Theme</label>
      <select id="themeSelect">
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </div>
  </header>
  <nav class="workspace-tabs" role="tablist" aria-label="Application workspaces">
    <button
      id="workbenchTab"
      class="active"
      type="button"
      role="tab"
      aria-selected="true"
      aria-controls="harvestSetup resultsPanel samplePanel"
    >
      Agentic Workbench
    </button>
    <button
      id="geometryTab"
      type="button"
      role="tab"
      aria-selected="false"
      aria-controls="geometryPanel"
    >
      Geometry Studio <span id="geometryTabBadge" class="tab-badge">0</span>
    </button>
    <button
      id="tableTab"
      type="button"
      role="tab"
      aria-selected="false"
      aria-controls="dataTablePanel"
    >
      Tabular Data <span id="tableTabBadge" class="tab-badge">0</span>
    </button>
  </nav>
  <main>
    <section id="harvestSetup" data-workspace="workbench">
      <h2>New Harvest</h2>
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
        <label for="profileSet">Facility Type</label>
        <select id="profileSet"></select>
      </div>

      <div id="campaignFacilityBlock" class="hidden">
        <label for="campaignFacilityTypes">Facility Types</label>
        <select id="campaignFacilityTypes" multiple></select>
      </div>

      <div id="subtypeBlock">
        <label for="profile">Subtype</label>
        <select id="profile"></select>
      </div>

      <div>
        <label for="countMethod">Count Mode</label>
        <select id="countMethod">
          <option value="">Profile default</option>
          <option value="direct_count">Direct count</option>
          <option value="population_subcomponent">Subcomponent count</option>
          <option value="hybrid">Hybrid</option>
        </select>
      </div>

      <div class="row">
        <div>
          <label for="target">Target</label>
          <input id="target" type="number" min="1" value="20">
        </div>
        <div>
          <label>Mode</label>
          <div class="mode">
            <button id="singleMode" class="active" type="button">Single</button>
            <button id="batchMode" type="button">Batch</button>
            <button id="campaignMode" type="button">Campaign</button>
          </div>
        </div>
      </div>

      <div class="actions">
        <button id="runFullPipelineButton" type="button">Run Full Pipeline</button>
        <button id="runButton" type="button">Run Harvest</button>
        <button id="refreshButton" class="secondary" type="button">Refresh Runs</button>
        <button id="clearRunsButton" class="secondary" type="button">Clear All</button>
      </div>
      <div class="pipeline-callout">
        <strong id="fullPipelineHeading">Guided end-to-end workflow</strong>
        <span id="fullPipelineStatus">
          Runs through review dataset assembly, then pauses for optional exclusions and approval.
        </span>
      </div>
      <div id="status" class="status">Ready.</div>
      <div class="history" id="history"></div>
    </section>

    <section id="resultsPanel" data-workspace="workbench">
      <h2>Results</h2>
      <div class="workflow-panel">
        <div class="workflow-header">
          <div>
            <h3>Project Workflow</h3>
            <div id="workflowSummary" class="workflow-summary">
              Start or select a harvest to see the full workflow.
            </div>
          </div>
          <button id="workflowNextButton" class="secondary" type="button" disabled>
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
        <button id="runQaqcButton" class="secondary" type="button">Run QAQC</button>
        <button id="runAddressButton" class="secondary" type="button">
          Run Address Enrichment
        </button>
        <button id="geocodeButton" class="secondary" type="button">
          Geocode All Accepted
        </button>
      </div>
      <details class="action-group">
        <summary>Prompts, JSON, and exports</summary>
        <div class="actions">
          <button id="copyButton" class="secondary" type="button">Copy JSON</button>
          <button id="copyQaqcButton" class="secondary" type="button">Copy QAQC Prompt</button>
          <button id="downloadJsonButton" class="secondary" type="button">
            Download Verified JSON
          </button>
          <button id="downloadCsvButton" class="secondary" type="button">
            Download Verified CSV
          </button>
        </div>
      </details>
      <textarea
        id="jsonOutput"
        spellcheck="false"
        placeholder="Harvest JSON will appear here."
      ></textarea>

      <div class="section-heading">
        <h2>Full Pipeline Transcript</h2>
        <button id="downloadTranscriptButton" class="secondary" type="button">
          Download Transcript (.txt)
        </button>
      </div>
      <textarea
        id="dialogueOutput"
        class="dialogue"
        spellcheck="false"
        readonly
        placeholder="The geographer, harvester, and review agents will report their findings here."
      ></textarea>

      <h2>Agent Activity</h2>
      <div class="actions">
        <button id="cancelButton" class="secondary" type="button" disabled>Cancel Run</button>
        <button id="exitButton" class="secondary" type="button">Exit Application</button>
      </div>
      <textarea
        id="activityOutput"
        class="activity"
        spellcheck="false"
        readonly
        placeholder="Agent activity will appear here while a harvest runs."
      ></textarea>
    </section>

    <section id="geometryPanel" class="wide hidden" data-workspace="geometry">
      <h2>Geometry Studio</h2>
      <div class="workflow-summary">
        Resolve coordinates, inspect spatial placement, digitize building footprints, and
        calculate planar area.
      </div>
      <div class="actions">
        <button id="loadApprovedButton" class="secondary" type="button">Load Approved</button>
        <button id="loadAugmentedSampleButton" class="secondary" type="button">
          Load Augmented Sample
        </button>
        <button id="saveFootprintButton" class="secondary" type="button">Save Footprint</button>
        <button id="skipGeometryButton" class="secondary" type="button">Skip</button>
      </div>
      <details class="action-group">
        <summary>Map view and geometry exports</summary>
        <div class="actions">
          <button id="showSampleExtentButton" class="secondary" type="button">
            Show Sample Extent
          </button>
          <button id="zoomSampleExtentButton" class="secondary" type="button">
            Zoom To Extent
          </button>
          <button id="clearSampleExtentButton" class="secondary" type="button">
            Clear Extent
          </button>
          <button id="downloadVerifiedJsonButton" class="secondary" type="button">
            Download Verified JSON
          </button>
          <button id="downloadVerifiedCsvButton" class="secondary" type="button">
            Download Verified CSV
          </button>
          <button id="downloadFootprintsButton" class="secondary" type="button">
            Download Footprints GeoJSON
          </button>
          <button id="downloadSampleFootprintsButton" class="secondary" type="button">
            Download Sample Footprints
          </button>
        </div>
      </details>
      <div class="extent-summary" id="geometryExtentSummary">
        Extent: load approved observations, then geocode or save points to map the sample.
      </div>
      <div class="status" id="geometryStatus">Load QAQC-approved observations to begin.</div>
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
            >
              Geocoded <span id="geocodedQueueCount">0</span>
            </button>
            <button
              id="manualQueueTab"
              type="button"
              role="tab"
              aria-selected="false"
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
              <button id="researchFacilityButton" class="secondary" type="button">
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
              <button id="previewCoordinatesButton" class="secondary" type="button">
                Preview Coordinate
              </button>
            </div>
            <div id="coordinatePasteStatus" class="status">
              Preview pasted coordinates before saving them.
            </div>
            <label for="manualAddress">Corrected Address or Place</label>
            <input id="manualAddress" placeholder="Enter a corrected facility address">
            <div class="actions">
              <button id="searchAddressButton" class="secondary" type="button">
                Search Corrected Address
              </button>
              <button id="placePointButton" class="secondary" type="button">
                Place Point on Map
              </button>
              <button id="useMapCenterButton" class="secondary" type="button">
                Place at Map Center
              </button>
              <button id="saveCoordinateButton" type="button">Save Coordinate</button>
            </div>
            <label for="coordinateReviewNotes">Coordinate Review Notes</label>
            <input
              id="coordinateReviewNotes"
              placeholder="Optional evidence or reasoning for the manual assignment"
            >
            <div id="coordinateDraftStatus" class="status">
              No coordinate change is waiting to be saved.
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

    <section id="dataTablePanel" class="wide hidden" data-workspace="table">
      <h2>Tabular Data</h2>
      <div class="workflow-summary" id="tableContext">
        Select a run or review dataset to inspect collected observations as rows.
      </div>
      <div class="table-toolbar">
        <div>
          <label>Rows</label>
          <div class="table-mode">
            <button id="tableVerifiedMode" class="active" type="button">Verified Only</button>
            <button id="tableAllMode" type="button">All Leads</button>
          </div>
        </div>
        <div>
          <label for="tableSearch">Search</label>
          <input id="tableSearch" placeholder="Filter visible rows">
        </div>
        <button id="tableClearSearchButton" class="secondary" type="button">Clear Search</button>
        <button id="tableRefreshButton" class="secondary" type="button">Refresh Table</button>
        <button id="tableCopyButton" class="secondary" type="button">Copy Visible Rows</button>
        <button id="tableCsvButton" class="secondary" type="button">Download CSV</button>
      </div>
      <div id="curationPanel" class="curation-panel hidden">
        <div class="workflow-header">
          <div>
            <h3>Human Curation</h3>
            <div id="curationSummary" class="workflow-summary">
              Approve all observations, or select only those that should be excluded.
            </div>
          </div>
          <button id="approveCurationButton" type="button">
            Approve Dataset &amp; Check Coverage
          </button>
        </div>
        <div class="curation-controls">
          <div>
            <label>Show</label>
            <div class="curation-filter">
              <button id="curationIncludedFilter" class="secondary active" type="button">
                Included
              </button>
              <button id="curationExcludedFilter" class="secondary" type="button">Excluded</button>
              <button id="curationAllFilter" class="secondary" type="button">All</button>
            </div>
          </div>
          <button id="selectVisibleButton" class="secondary" type="button">Select Visible</button>
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
          <button id="excludeSelectedButton" class="secondary" type="button">
            Exclude Selected
          </button>
          <button id="restoreSelectedButton" class="secondary" type="button">
            Restore Selected
          </button>
        </div>
        <div id="curationStatus" class="status">
          No individual review is required. Approval with no exclusions is valid.
        </div>
      </div>
      <div class="summary">
        <div class="metric"><span>Context</span><strong id="tableMetricContext">-</strong></div>
        <div class="metric"><span>Mode</span><strong id="tableMetricMode">Verified</strong></div>
        <div class="metric"><span>Rows</span><strong id="tableMetricRows">0</strong></div>
        <div class="metric"><span>Visible</span><strong id="tableMetricVisible">0</strong></div>
      </div>
      <div id="tableStatus" class="status">Ready.</div>
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

    <section id="samplePanel" class="wide" data-workspace="workbench">
      <h2>Review Dataset / Coverage</h2>
      <div class="actions">
        <button id="createSampleButton" class="secondary" type="button">
          Assemble Review Dataset
        </button>
        <button id="analyzeCoverageButton" class="secondary" type="button">
          Check Coverage
        </button>
        <button id="runGapFillButton" class="secondary" type="button">
          Run Targeted Follow-ups
        </button>
      </div>
      <details class="action-group">
        <summary>Repair passes and review dataset exports</summary>
        <div class="actions">
          <button id="runSampleQaqcButton" class="secondary" type="button">
            Run QAQC Missing
          </button>
          <button id="runSampleAddressButton" class="secondary" type="button">
            Run Address Missing
          </button>
          <button id="downloadSampleJsonButton" class="secondary" type="button">
            Download Sample JSON
          </button>
          <button id="downloadSampleCsvButton" class="secondary" type="button">
            Download Sample CSV
          </button>
        </div>
      </details>
      <div class="status" id="sampleStatus">
        Assemble a review dataset after geometry review; coverage works best once approved
        observations have geocoded points.
      </div>
      <textarea
        id="sampleOutput"
        class="compact"
        spellcheck="false"
        readonly
        placeholder="Review dataset and coverage output will appear here."
      ></textarea>
    </section>
  </main>
  <script src="/assets/app.js"></script>
</body>
</html>
"""
