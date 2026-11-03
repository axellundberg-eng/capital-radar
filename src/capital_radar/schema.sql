-- Fryst schema för Capital Radar, genererat från Screener-analys-hus.
-- Kompatibelt med produktionsdatabasen: allt skapas med IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS issuers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    jurisdiction_code TEXT,
    lei TEXT UNIQUE,
    org_number TEXT,
    identity_status TEXT NOT NULL DEFAULT 'provisional',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    event_key TEXT NOT NULL UNIQUE,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    language TEXT,
    category TEXT,
    profile_category TEXT NOT NULL DEFAULT 'other',
    cash_relevance_score INTEGER NOT NULL DEFAULT 0,
    relevance_reason TEXT NOT NULL,
    queued_for_analysis INTEGER NOT NULL DEFAULT 0,
    analysis_status TEXT NOT NULL DEFAULT 'metadata_only',
    content_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    url TEXT NOT NULL,
    file_name TEXT,
    mime_type TEXT,
    sha256 TEXT,
    downloaded_at TEXT,
    UNIQUE(document_id, url)
);

CREATE TABLE IF NOT EXISTS document_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    attachment_id INTEGER REFERENCES attachments(id),
    source_url TEXT NOT NULL,
    final_url TEXT,
    local_path TEXT,
    mime_type TEXT,
    sha256 TEXT,
    byte_size INTEGER,
    extraction_method TEXT,
    extracted_text TEXT,
    text_sha256 TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER,
    quality_score INTEGER NOT NULL DEFAULT 0,
    quality_status TEXT NOT NULL DEFAULT 'unscored',
    quality_reason TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(document_id, source_url)
);

CREATE TABLE IF NOT EXISTS extraction_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    content_id INTEGER NOT NULL REFERENCES document_contents(id),
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    selected_char_count INTEGER NOT NULL,
    estimated_input_tokens INTEGER NOT NULL,
    status TEXT NOT NULL,
    response_id TEXT,
    finish_reason TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    total_tokens INTEGER,
    raw_response_json TEXT,
    parsed_output_json TEXT,
    error TEXT,
    lease_token TEXT,
    deadline_at TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(document_id, model, prompt_version, input_sha256)
);

CREATE TABLE IF NOT EXISTS manual_action_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'queued',
    lease_token TEXT,
    deadline_at TEXT,
    started_at TEXT NOT NULL,
    updated_at TEXT,
    completed_at TEXT,
    result_json TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS action_progress_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id INTEGER NOT NULL REFERENCES manual_action_runs(id),
    sequence_no INTEGER NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT,
    document_id INTEGER REFERENCES documents(id),
    extraction_job_id INTEGER REFERENCES extraction_jobs(id),
    estimated_tokens INTEGER,
    actual_tokens INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(action_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS adjusted_runway_recalculations (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 idempotency_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 calculation_hash TEXT NOT NULL,
 correction_ids_json TEXT NOT NULL,
 before_json TEXT NOT NULL,
 after_json TEXT NOT NULL,
 actor TEXT NOT NULL,
 reason TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
 created_at TEXT NOT NULL,
 UNIQUE(issuer_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER REFERENCES issuers(id),
    case_id TEXT,
    agent_type TEXT NOT NULL,
    artifact_kind TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    confidence REAL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    source_data_cutoff TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(agent_type, artifact_kind, content_sha256)
);

CREATE TABLE IF NOT EXISTS financing_transaction_corrections (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 proposal_key TEXT NOT NULL UNIQUE,
 correction_type TEXT NOT NULL,
 transaction_id TEXT NOT NULL,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 expected_document_ids_json TEXT NOT NULL,
 additional_document_ids_json TEXT NOT NULL DEFAULT '[]',
 resulting_rounds_json TEXT NOT NULL,
 reason TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'proposed',
 original_feed_json TEXT NOT NULL,
 applied_feed_json TEXT,
 created_by TEXT NOT NULL,
 approved_by TEXT,
 applied_by TEXT,
 reverted_by TEXT,
 created_at TEXT NOT NULL,
 approved_at TEXT,
 applied_at TEXT,
 reverted_at TEXT,
 CHECK(correction_type IN (
   'split_transaction','move_document','assign_document_to_round',
   'unlink_document_from_transaction','rebuild_transaction'
 )),
 CHECK(status IN ('proposed','approved','applied','reverted'))
);

CREATE TABLE IF NOT EXISTS financing_transaction_value_corrections (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 proposal_key TEXT NOT NULL UNIQUE,
 transaction_id TEXT NOT NULL,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 field_name TEXT NOT NULL,
 expected_previous_value_json TEXT NOT NULL,
 proposed_value_json TEXT NOT NULL,
 source_document_id INTEGER NOT NULL REFERENCES documents(id),
 source_excerpt TEXT NOT NULL,
 confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
 rationale TEXT NOT NULL,
 original_transaction_json TEXT NOT NULL,
 effective_transaction_json TEXT,
 status TEXT NOT NULL DEFAULT 'proposed'
   CHECK(status IN ('proposed','approved','applied','reverted','failed')),
 created_by TEXT NOT NULL,
 approved_by TEXT,
 applied_by TEXT,
 reverted_by TEXT,
 created_at TEXT NOT NULL,
 approved_at TEXT,
 applied_at TEXT,
 reverted_at TEXT,
 last_apply_error TEXT
);

CREATE TABLE IF NOT EXISTS agent_proposal_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, request_json TEXT NOT NULL, request_sha256 TEXT NOT NULL,
 issuer_id INTEGER NOT NULL, transaction_id TEXT NOT NULL, source_document_id INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued'
 CHECK(status IN ('queued','validating','proposed','rejected','failed')),
 correction_id INTEGER REFERENCES financing_transaction_value_corrections(id),
 error_code TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
 available_at REAL NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL,
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, grouping_correction_id INTEGER REFERENCES financing_transaction_corrections(id),
 UNIQUE(actor,idempotency_key)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT,
    issuer_id INTEGER REFERENCES issuers(id),
    deal_id INTEGER,
    agent_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    thinking_enabled INTEGER NOT NULL DEFAULT 0,
    reasoning_effort TEXT,
    prompt_version TEXT NOT NULL,
    schema_version TEXT NOT NULL DEFAULT 'v1',
    execution_profile_version TEXT NOT NULL DEFAULT 'v1',
    execution_profile_json TEXT NOT NULL DEFAULT '{}',
    input_hash TEXT NOT NULL,
    source_data_cutoff TEXT,
    status TEXT NOT NULL
        CHECK (status IN ('queued','running','completed','failed','skipped_idempotent','unavailable','blocked')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    latency_ms INTEGER,
    input_tokens INTEGER,
    cached_input_tokens INTEGER,
    output_tokens INTEGER,
    estimated_cost_usd REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    provider_request_id TEXT,
    confidence REAL,
    artifact_id INTEGER REFERENCES agent_artifacts(id),
    error_type TEXT,
    safe_error_message TEXT,
    idempotency_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(idempotency_key)
);

CREATE TABLE IF NOT EXISTS runway_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    as_of_date TEXT NOT NULL,
    report_document_id INTEGER NOT NULL REFERENCES documents(id),
    report_period_end TEXT NOT NULL,
    currency TEXT NOT NULL,
    baseline_cash REAL NOT NULL,
    operating_cash_flow REAL NOT NULL,
    period_months REAL NOT NULL,
    monthly_operating_burn REAL,
    confirmed_post_report_adjustment REAL NOT NULL,
    committed_liquidity REAL NOT NULL,
    conditional_liquidity REAL NOT NULL,
    adjusted_cash REAL NOT NULL,
    weighted_liquidity REAL NOT NULL,
    operating_runway_months REAL,
    adjusted_runway_months REAL,
    operating_need_score INTEGER NOT NULL,
    financing_adjustment_points INTEGER NOT NULL,
    adjusted_need_score INTEGER NOT NULL,
    need_timing TEXT NOT NULL,
    alert_level TEXT NOT NULL,
    evidence_posture TEXT NOT NULL,
    calculation_json TEXT NOT NULL,
    calculation_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capital_need_interpretations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    as_of_date TEXT NOT NULL,
    method TEXT NOT NULL,
    need_level TEXT NOT NULL,
    need_score INTEGER NOT NULL,
    need_timing TEXT NOT NULL,
    headline TEXT NOT NULL,
    summary TEXT NOT NULL,
    capital_need TEXT NOT NULL,
    transaction_context TEXT,
    recommended_action TEXT NOT NULL,
    cash_amount REAL,
    operating_cash_flow REAL,
    period_months REAL,
    currency TEXT,
    report_period_end TEXT,
    confidence REAL NOT NULL,
    evidence_posture TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    source_document_ids_json TEXT NOT NULL,
    interpretation_sha256 TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    version_no INTEGER NOT NULL,
    interpretation_id INTEGER REFERENCES capital_need_interpretations(id),
    runway_assessment_id INTEGER REFERENCES runway_assessments(id),
    action_id INTEGER REFERENCES manual_action_runs(id),
    status TEXT NOT NULL,
    capital_need_decision TEXT NOT NULL DEFAULT 'unknown',
    need_score INTEGER,
    need_timing TEXT,
    runway_months REAL,
    alert_level TEXT,
    bergs_fit_score INTEGER,
    bergs_fit_summary TEXT,
    headline TEXT,
    confidence REAL,
    source_as_of_date TEXT,
    source_document_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(issuer_id, version_no),
    UNIQUE(interpretation_id)
);

CREATE TABLE IF NOT EXISTS document_evidence_texts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    content_id INTEGER REFERENCES document_contents(id),
    text_sha256 TEXT NOT NULL,
    full_text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    page_count INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(document_id, text_sha256)
);

CREATE TABLE IF NOT EXISTS document_evidence_pages (
    evidence_text_id INTEGER NOT NULL REFERENCES document_evidence_texts(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    page_text TEXT NOT NULL,
    PRIMARY KEY(evidence_text_id, page_number)
);

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    base_url TEXT NOT NULL,
    evidence_tier INTEGER NOT NULL,
    authority TEXT NOT NULL,
    access_status TEXT NOT NULL,
    terms_status TEXT NOT NULL,
    notes TEXT,
    last_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_external_id TEXT NOT NULL,
    discovered_url TEXT NOT NULL,
    source_published_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE(source_id, source_external_id)
);

CREATE TABLE IF NOT EXISTS financial_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    extraction_job_id INTEGER NOT NULL REFERENCES extraction_jobs(id),
    fact_type TEXT NOT NULL,
    reported_value REAL,
    normalized_value REAL,
    currency TEXT,
    reported_unit TEXT,
    period_start TEXT,
    period_end TEXT,
    as_of_date TEXT,
    evidence_label TEXT NOT NULL,
    source_page INTEGER,
    source_excerpt TEXT NOT NULL,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financing_backfill_state (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    requested_start_date TEXT,
    requested_end_date TEXT,
    oldest_fetched_date TEXT,
    newest_fetched_date TEXT,
    source TEXT,
    cursor TEXT,
    checkpoint TEXT,
    last_successful_fetch_at TEXT,
    last_successful_extraction_at TEXT,
    last_successful_normalization_at TEXT,
    incomplete_ranges_json TEXT NOT NULL DEFAULT '[]',
    failure_count INTEGER NOT NULL DEFAULT 0,
    retry_state TEXT,
    status TEXT NOT NULL DEFAULT 'not_started',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financing_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    event_fingerprint TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    description TEXT NOT NULL,
    announcement_date TEXT,
    effective_date TEXT,
    status TEXT NOT NULL,
    cash_direction TEXT NOT NULL,
    gross_amount REAL,
    net_amount REAL,
    cash_effect_amount REAL,
    currency TEXT,
    liquidity_effect_kind TEXT NOT NULL,
    committed_amount REAL,
    conditional_amount REAL,
    reflected_in_report TEXT NOT NULL,
    conditions TEXT,
    use_of_proceeds TEXT,
    confidence REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
, canonical_round_key TEXT, lifecycle_stage TEXT, stage_effective_at TEXT, financing_type TEXT, raw_type_description TEXT, issue_price REAL, shares_before REAL, shares_issued REAL, shares_after REAL, dilution REAL, market_price_reference REAL, vwap_reference REAL, market_cap_before REAL, raise_to_market_cap REAL, discount_to_spot REAL, discount_to_vwap REAL, subscription_commitment_amount REAL, subscription_commitment_pct REAL, guarantee_commitment_amount REAL, guarantee_commitment_pct REAL, secured_total_amount REAL, secured_total_pct REAL, final_subscription_amount REAL, final_subscription_pct REAL, subscription_without_guarantees_pct REAL, guarantee_utilized_amount REAL, guarantee_utilized_pct REAL, guarantee_fee_details TEXT, guarantee_fee_amount REAL, guarantee_fee_pct REAL, guarantee_fee_cash_pct REAL, guarantee_fee_share_pct REAL, guarantee_fee_basis TEXT, credit_limit REAL, drawn_amount REAL, undrawn_commitment REAL, transaction_cost REAL, transaction_cost_pct REAL, adviser TEXT, issuer_agent TEXT, outcome TEXT, extraction_version TEXT, normalization_version TEXT);

CREATE TABLE IF NOT EXISTS financing_correction_proposals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_key TEXT NOT NULL UNIQUE,
 issuer_id INTEGER NOT NULL REFERENCES issuers(id), financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
 canonical_round_key TEXT, correction_type TEXT NOT NULL,
 field_name TEXT, previous_value_json TEXT, proposed_value_json TEXT,
 expected_previous_value_json TEXT, source_document_id INTEGER REFERENCES documents(id),
 source_excerpt TEXT, confidence REAL NOT NULL, rationale TEXT NOT NULL,
 impact_preview_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'proposed',
 created_by TEXT NOT NULL, approved_by TEXT, applied_by TEXT, created_at TEXT NOT NULL,
 approved_at TEXT, applied_at TEXT, reverted_at TEXT, resolution_note TEXT,
 CHECK(correction_type IN ('fill_field','replace_field','clear_field','change_lifecycle','assign_round','merge_events','supersede_duplicate','exclude_cash_effect','restore_cash_effect')),
 CHECK(status IN ('proposed','approved','applied','rejected','reverted','superseded'))
);

CREATE TABLE IF NOT EXISTS financing_event_enrichment_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    field_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_document_id INTEGER NOT NULL REFERENCES documents(id),
    source_excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(financing_event_id, field_name)
);

CREATE TABLE IF NOT EXISTS financing_event_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    extraction_job_id INTEGER NOT NULL REFERENCES extraction_jobs(id),
    evidence_label TEXT NOT NULL,
    source_page INTEGER,
    source_excerpt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(financing_event_id, document_id)
);

CREATE TABLE IF NOT EXISTS financing_event_guarantee_tranches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    tier TEXT NOT NULL CHECK(tier IN ('top', 'middle', 'bottom', 'other')),
    commitment_amount REAL,
    fee_rate REAL,
    fee_amount REAL,
    utilized_amount REAL,
    source_document_id INTEGER REFERENCES documents(id),
    source_page INTEGER,
    source_excerpt TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(financing_event_id, tier, source_document_id)
);

CREATE TABLE IF NOT EXISTS financing_event_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    raw_name TEXT NOT NULL,
    normalized_candidate TEXT,
    role TEXT NOT NULL,
    amount REAL,
    percentage REAL,
    source TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    resolution_status TEXT NOT NULL DEFAULT 'unresolved',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(financing_event_id, raw_name, role)
);

CREATE TABLE IF NOT EXISTS financing_event_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    related_financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    relationship_type TEXT NOT NULL,
    source TEXT,
    confidence REAL NOT NULL DEFAULT 0.0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(financing_event_id, related_financing_event_id, relationship_type),
    CHECK(financing_event_id <> related_financing_event_id)
);

CREATE TABLE IF NOT EXISTS financing_field_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_key TEXT NOT NULL UNIQUE,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    field_name TEXT NOT NULL,
    proposed_value_json TEXT NOT NULL,
    previous_value_json TEXT,
    source_document_id INTEGER NOT NULL REFERENCES documents(id),
    source_excerpt TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK(status IN ('proposed', 'applied', 'rejected', 'superseded')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    resolution_note TEXT,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    as_of_date TEXT NOT NULL,
    price REAL,
    market_cap REAL,
    adtv REAL,
    vwap REAL,
    return_m20 REAL,
    return_p1 REAL,
    return_p5 REAL,
    return_p20 REAL,
    return_p60 REAL,
    source TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(issuer_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS financing_notification_corrections(
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, issuer_id INTEGER NOT NULL,
 group_key TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL, rationale TEXT NOT NULL, created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS financing_notification_members(
 transaction_id TEXT PRIMARY KEY, issuer_id INTEGER NOT NULL, group_key TEXT NOT NULL,
 correction_id INTEGER NOT NULL REFERENCES financing_notification_corrections(id),
 ordinal INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS financing_outcome_market_days (
    financing_event_id INTEGER NOT NULL REFERENCES financing_events(id),
    trading_date TEXT NOT NULL,
    close REAL NOT NULL,
    volume REAL,
    source TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    PRIMARY KEY(financing_event_id, trading_date)
);

CREATE TABLE IF NOT EXISTS financing_outcome_market_metrics (
    financing_event_id INTEGER PRIMARY KEY REFERENCES financing_events(id),
    outcome_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    close_average_5d REAL,
    volume_weighted_close_5d REAL,
    total_volume_5d REAL,
    trading_dates_json TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS financing_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id INTEGER NOT NULL UNIQUE REFERENCES financing_correction_proposals(id),
 financing_event_id INTEGER NOT NULL REFERENCES financing_events(id), field_name TEXT NOT NULL,
 override_value_json TEXT, source_document_id INTEGER REFERENCES documents(id), source_excerpt TEXT,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_round_memberships (
 id INTEGER PRIMARY KEY AUTOINCREMENT, issuer_id INTEGER NOT NULL REFERENCES issuers(id), canonical_round_key TEXT NOT NULL,
 financing_event_id INTEGER NOT NULL REFERENCES financing_events(id), lifecycle_stage TEXT, language TEXT,
 is_primary INTEGER NOT NULL DEFAULT 0, superseded_by_event_id INTEGER REFERENCES financing_events(id),
 cash_effect_included INTEGER NOT NULL DEFAULT 1, proposal_id INTEGER REFERENCES financing_correction_proposals(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(issuer_id, canonical_round_key, financing_event_id)
);

CREATE TABLE IF NOT EXISTS financing_transaction_document_links (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_value_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 document_id INTEGER NOT NULL REFERENCES documents(id),
 canonical_round_key TEXT NOT NULL,
 category TEXT NOT NULL,
 financing_event_id INTEGER REFERENCES financing_events(id),
 transaction_id TEXT,
 relation_action TEXT NOT NULL DEFAULT 'link',
 active INTEGER NOT NULL DEFAULT 1,
 deactivated_by_correction_id INTEGER REFERENCES financing_transaction_value_corrections(id),
 created_at TEXT NOT NULL,
 reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_transaction_document_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 document_id INTEGER NOT NULL REFERENCES documents(id),
 canonical_round_key TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_transaction_grouping_consolidations (
 correction_id INTEGER PRIMARY KEY REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id), plan_json TEXT NOT NULL,
 fingerprints_json TEXT NOT NULL, max_value_row_id INTEGER NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_transaction_grouping_replacements (
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_corrections(id),
 previous_override_id INTEGER NOT NULL REFERENCES financing_transaction_document_overrides(id),
 PRIMARY KEY(correction_id,previous_override_id)
);

CREATE TABLE IF NOT EXISTS financing_transaction_instrument_bindings (
 correction_id INTEGER PRIMARY KEY REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 transaction_id TEXT NOT NULL, category TEXT NOT NULL,
 canonical_round_key TEXT NOT NULL, document_ids_json TEXT NOT NULL,
 source_fingerprints_json TEXT NOT NULL,
 preserved_membership_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_transaction_projection_bindings (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL UNIQUE REFERENCES financing_transaction_corrections(id),
 issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 canonical_round_key TEXT NOT NULL,
 transaction_id TEXT NOT NULL,
 category TEXT NOT NULL,
 document_ids_json TEXT NOT NULL,
 neutral_document_ids_json TEXT NOT NULL,
 neutral_source_fingerprints_json TEXT NOT NULL,
 retired_transaction_ids_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS financing_transaction_stage_evidence (
 correction_id INTEGER PRIMARY KEY REFERENCES financing_transaction_value_corrections(id),
 evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS financing_transaction_value_overrides (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 correction_id INTEGER NOT NULL REFERENCES financing_transaction_value_corrections(id),
 transaction_id TEXT NOT NULL,
 field_name TEXT NOT NULL,
 value_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 deactivated_by_correction_id INTEGER REFERENCES financing_transaction_value_corrections(id),
 created_at TEXT NOT NULL,
 reverted_at TEXT
);

CREATE TABLE IF NOT EXISTS issuer_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    external_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source_id, normalized_alias),
    UNIQUE(source_id, external_key)
);

CREATE TABLE IF NOT EXISTS issuer_analysis_summary (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    current_analysis_version_id INTEGER REFERENCES analysis_versions(id),
    current_interpretation_id INTEGER REFERENCES capital_need_interpretations(id),
    current_runway_assessment_id INTEGER REFERENCES runway_assessments(id),
    last_action_id INTEGER REFERENCES manual_action_runs(id),
    analysis_status TEXT NOT NULL DEFAULT 'not_analyzed',
    capital_need_decision TEXT NOT NULL DEFAULT 'unknown',
    need_score INTEGER,
    need_timing TEXT,
    runway_months REAL,
    alert_level TEXT,
    bergs_fit_score INTEGER,
    headline TEXT,
    confidence REAL,
    source_as_of_date TEXT,
    last_success_at TEXT,
    last_attempt_at TEXT,
    failure_reason TEXT,
    updated_at TEXT NOT NULL
, operating_runway_months REAL, adjusted_runway_months REAL, operating_need_score INTEGER, adjusted_need_score INTEGER, financing_adjustment_points INTEGER, interpretation_headline TEXT, recommended_action TEXT, evidence_posture TEXT, effective_need_score INTEGER, effective_alert_level TEXT, interpreted_at TEXT, last_mfn_refresh_at TEXT, watching INTEGER DEFAULT 0, owner TEXT, next_action TEXT, follow_up_at TEXT);

CREATE TABLE IF NOT EXISTS issuer_crm (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    status TEXT NOT NULL DEFAULT 'missing' CHECK(status IN ('missing','watching','contacted','regular_contact','ca_customer')),
    owner TEXT,
    next_follow_up_at TEXT,
    next_action TEXT,
    activity_type TEXT CHECK(activity_type IN ('call','meeting','email','research','other')),
    last_ai_update_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issuer_report_refresh_state (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    last_checked_at TEXT,
    last_status TEXT,
    last_report_document_id INTEGER REFERENCES documents(id),
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issuer_runway_snapshots (
 issuer_id INTEGER NOT NULL REFERENCES issuers(id), snapshot_date TEXT NOT NULL,
 reported_runway_months REAL, adjusted_runway_months REAL, runway_band TEXT,
 cash REAL, monthly_burn REAL, calculation_version TEXT NOT NULL,
 calculation_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(issuer_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS issuer_screening_overrides (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    eligible INTEGER NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issuer_watchlist (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    active INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_mfn_refresh_at TEXT,
    last_interpretation_at TEXT
);

CREATE TABLE IF NOT EXISTS issuer_workflow (
    issuer_id INTEGER PRIMARY KEY REFERENCES issuers(id),
    status TEXT NOT NULL DEFAULT 'new',
    owner TEXT,
    next_action TEXT,
    follow_up_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS radar_financing_weights (
 id INTEGER PRIMARY KEY, issuer_id INTEGER NOT NULL REFERENCES issuers(id),
 transaction_id TEXT NOT NULL, weight REAL NOT NULL CHECK(weight BETWEEN 0 AND 1),
 actor TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    document_id INTEGER NOT NULL REFERENCES documents(id) UNIQUE,
    extraction_job_id INTEGER NOT NULL REFERENCES extraction_jobs(id),
    is_financial_report INTEGER NOT NULL,
    report_period_start TEXT,
    report_period_end TEXT,
    publication_date TEXT,
    currency TEXT,
    reported_scale TEXT,
    period_months REAL,
    confidence REAL NOT NULL,
    requires_human_review INTEGER NOT NULL,
    uncertainties_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS report_refresh_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_kind TEXT NOT NULL,
    collection_scope TEXT NOT NULL DEFAULT 'all_active_issuers',
    force_backfill INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    lease_token TEXT,
    deadline_at TEXT,
    global_discovery_status TEXT NOT NULL DEFAULT 'pending',
    global_discovery_json TEXT,
    total_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS report_refresh_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES report_refresh_runs(id),
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    documents_seen INTEGER NOT NULL DEFAULT 0,
    documents_prepared INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(run_id, issuer_id)
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS screening_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    refresh_mfn INTEGER NOT NULL DEFAULT 1,
    prepared_only INTEGER NOT NULL DEFAULT 1,
    max_api_calls INTEGER NOT NULL DEFAULT 10,
    api_calls_used INTEGER NOT NULL DEFAULT 0,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS screening_batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES screening_batches(id),
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    action_id INTEGER REFERENCES manual_action_runs(id),
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    decision TEXT,
    need_score INTEGER,
    need_timing TEXT,
    runway_months REAL,
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(batch_id, issuer_id),
    UNIQUE(action_id)
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    run_type TEXT NOT NULL,
    source_url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    http_status INTEGER,
    items_seen INTEGER NOT NULL DEFAULT 0,
    items_upserted INTEGER NOT NULL DEFAULT 0,
    items_matched INTEGER NOT NULL DEFAULT 0,
    items_queued INTEGER NOT NULL DEFAULT 0,
    response_sha256 TEXT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS securities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer_id INTEGER NOT NULL REFERENCES issuers(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT NOT NULL,
    isin TEXT NOT NULL,
    symbol TEXT NOT NULL,
    display_name TEXT NOT NULL,
    currency TEXT,
    market TEXT NOT NULL,
    segment TEXT NOT NULL,
    mic TEXT,
    jurisdiction_code TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id INTEGER REFERENCES source_runs(id),
    metadata_json TEXT NOT NULL,
    UNIQUE(source_id, external_id)
);

CREATE TABLE IF NOT EXISTS universe_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    total_issuers INTEGER NOT NULL,
    total_securities INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    source_run_ids_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS universe_snapshot_members (
    snapshot_id INTEGER NOT NULL REFERENCES universe_snapshots(id),
    security_id INTEGER NOT NULL REFERENCES securities(id),
    PRIMARY KEY(snapshot_id, security_id)
);

CREATE INDEX IF NOT EXISTS adjusted_runway_recalculations_issuer
 ON adjusted_runway_recalculations(issuer_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_action_progress_events_cursor
    ON action_progress_events(action_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_case
    ON agent_artifacts(case_id, agent_type);

CREATE INDEX IF NOT EXISTS idx_agent_artifacts_issuer
    ON agent_artifacts(issuer_id, agent_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_proposal_queue ON agent_proposal_jobs(status,available_at,id);

CREATE INDEX IF NOT EXISTS idx_agent_review_financing_document ON financing_event_evidence(document_id);

CREATE INDEX IF NOT EXISTS idx_agent_runs_case
    ON agent_runs(case_id, agent_type);

CREATE INDEX IF NOT EXISTS idx_agent_runs_issuer
    ON agent_runs(issuer_id, agent_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status
    ON agent_runs(status, started_at);

CREATE INDEX IF NOT EXISTS idx_analysis_versions_latest
    ON analysis_versions(issuer_id, version_no DESC);

CREATE INDEX IF NOT EXISTS idx_capital_need_interpretations_latest
    ON capital_need_interpretations(issuer_id, created_at);

CREATE INDEX IF NOT EXISTS idx_document_contents_status
    ON document_contents(status, document_id);

CREATE INDEX IF NOT EXISTS idx_document_evidence_texts_document
    ON document_evidence_texts(document_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_document_sources_document_source
    ON document_sources(document_id, source_id);

CREATE INDEX IF NOT EXISTS idx_documents_issuer_published
    ON documents(issuer_id, published_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_documents_news_order
    ON documents(published_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_documents_queue
    ON documents(queued_for_analysis, analysis_status, published_at);

CREATE INDEX IF NOT EXISTS idx_extraction_jobs_status
    ON extraction_jobs(status, document_id);

CREATE INDEX IF NOT EXISTS idx_financial_facts_lookup
    ON financial_facts(issuer_id, fact_type, period_end, as_of_date);

CREATE INDEX IF NOT EXISTS idx_financing_correction_proposals_queue ON financing_correction_proposals(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_financing_enrichment_event
    ON financing_event_enrichment_facts(financing_event_id, field_name);

CREATE INDEX IF NOT EXISTS idx_financing_events_canonical_round
    ON financing_events(issuer_id, canonical_round_key, lifecycle_stage);

CREATE INDEX IF NOT EXISTS idx_financing_events_issuer_date
    ON financing_events(issuer_id, effective_date, announcement_date);

CREATE INDEX IF NOT EXISTS idx_financing_field_proposals_event
    ON financing_field_proposals(financing_event_id, field_name, status);

CREATE INDEX IF NOT EXISTS idx_financing_field_proposals_queue
    ON financing_field_proposals(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_financing_guarantee_tranches_event
    ON financing_event_guarantee_tranches(financing_event_id, tier);

CREATE INDEX IF NOT EXISTS idx_financing_outcome_market_days_date
    ON financing_outcome_market_days(trading_date);

CREATE UNIQUE INDEX IF NOT EXISTS idx_financing_override_active ON financing_overrides(financing_event_id, field_name) WHERE active=1;

CREATE INDEX IF NOT EXISTS idx_financing_participants_event
    ON financing_event_participants(financing_event_id, role);

CREATE INDEX IF NOT EXISTS idx_financing_relationships_related
    ON financing_event_relationships(related_financing_event_id, relationship_type);

CREATE UNIQUE INDEX IF NOT EXISTS idx_financing_round_primary ON financing_round_memberships(issuer_id, canonical_round_key) WHERE is_primary=1;

CREATE INDEX IF NOT EXISTS idx_issuer_alias_normalized
    ON issuer_aliases(normalized_alias);

CREATE INDEX IF NOT EXISTS idx_issuer_analysis_priority
    ON issuer_analysis_summary(
        capital_need_decision, need_score DESC, runway_months
    );

CREATE INDEX IF NOT EXISTS idx_issuer_crm_status_follow_up
    ON issuer_crm(status, next_follow_up_at);

CREATE INDEX IF NOT EXISTS idx_issuer_workflow_follow_up
    ON issuer_workflow(status, follow_up_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_action_active_unique
    ON manual_action_runs(issuer_id, action_type)
    WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_manual_action_runs
    ON manual_action_runs(issuer_id, started_at);

CREATE INDEX IF NOT EXISTS idx_report_extractions_period
    ON report_extractions(issuer_id, report_period_end);

CREATE UNIQUE INDEX IF NOT EXISTS idx_report_refresh_active_unique
    ON report_refresh_runs((1))
    WHERE status IN ('queued', 'running', 'paused');

CREATE INDEX IF NOT EXISTS idx_report_refresh_items_progress
    ON report_refresh_items(run_id, status, id);

CREATE INDEX IF NOT EXISTS idx_runway_latest
    ON runway_assessments(issuer_id, created_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_screening_batch_active_unique
    ON screening_batches((1))
    WHERE status IN ('queued', 'running', 'stopping');

CREATE INDEX IF NOT EXISTS idx_screening_batch_items_progress
    ON screening_batch_items(batch_id, status, id);

CREATE INDEX IF NOT EXISTS idx_securities_isin ON securities(isin);

CREATE INDEX IF NOT EXISTS idx_securities_market_segment
    ON securities(market, segment, active);

CREATE INDEX IF NOT EXISTS idx_transaction_corrections_status
 ON financing_transaction_corrections(status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_document_link_active
 ON financing_transaction_document_links(document_id,canonical_round_key,category)
 WHERE active=1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_document_override_active
 ON financing_transaction_document_overrides(document_id) WHERE active=1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_value_override_active
 ON financing_transaction_value_overrides(transaction_id,field_name) WHERE active=1;

CREATE INDEX IF NOT EXISTS idx_transaction_value_status
 ON financing_transaction_value_corrections(status,created_at DESC);

CREATE INDEX IF NOT EXISTS notification_members_issuer ON financing_notification_members(issuer_id,group_key);

CREATE INDEX IF NOT EXISTS radar_weight_latest
 ON radar_financing_weights(transaction_id, id);

CREATE TRIGGER IF NOT EXISTS adjusted_runway_recalculations_no_delete
 BEFORE DELETE ON adjusted_runway_recalculations BEGIN
 SELECT RAISE(ABORT, 'runway recalculation audits are immutable'); END;

CREATE TRIGGER IF NOT EXISTS adjusted_runway_recalculations_no_update
 BEFORE UPDATE ON adjusted_runway_recalculations BEGIN
 SELECT RAISE(ABORT, 'runway recalculation audits are immutable'); END;

CREATE TRIGGER IF NOT EXISTS agent_proposal_request_immutable
 BEFORE UPDATE OF actor,idempotency_key,request_json,request_sha256,issuer_id,transaction_id,source_document_id,created_at
 ON agent_proposal_jobs BEGIN SELECT RAISE(ABORT,'immutable_agent_request'); END;

CREATE TRIGGER IF NOT EXISTS agent_proposal_request_no_delete BEFORE DELETE ON agent_proposal_jobs
 BEGIN SELECT RAISE(ABORT,'immutable_agent_request'); END;

CREATE TRIGGER IF NOT EXISTS notification_audit_no_delete BEFORE DELETE ON financing_notification_corrections
 BEGIN SELECT RAISE(ABORT,'immutable_notification_audit'); END;

CREATE TRIGGER IF NOT EXISTS notification_audit_no_update BEFORE UPDATE ON financing_notification_corrections
 BEGIN SELECT RAISE(ABORT,'immutable_notification_audit'); END;

CREATE TRIGGER IF NOT EXISTS radar_snapshot_no_delete
 BEFORE DELETE ON issuer_runway_snapshots BEGIN
 SELECT RAISE(ABORT, 'runway snapshots are immutable'); END;

CREATE TRIGGER IF NOT EXISTS radar_snapshot_no_update
 BEFORE UPDATE ON issuer_runway_snapshots BEGIN
 SELECT RAISE(ABORT, 'runway snapshots are immutable'); END;

-- Oberoende granskare (tillägg efter frysningen). Granskaren skriver ALDRIG
-- i DeepSeeks tabeller; den lägger bara egna rader här. Ett run = högst ett
-- provideranrop. idempotency_key gör att samma indata aldrig granskas två
-- gånger med samma modell och promptversion.
CREATE TABLE IF NOT EXISTS review_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    issuer_id INTEGER,
    document_id INTEGER,
    extraction_job_id INTEGER,
    status TEXT NOT NULL,
    error_type TEXT,
    error TEXT,
    response_id TEXT,
    stop_reason TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    raw_output TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1,
    deadline_at TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS review_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES review_runs(id),
    task TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    document_id INTEGER,
    extraction_job_id INTEGER,
    verdict TEXT NOT NULL,
    issues_json TEXT NOT NULL DEFAULT '[]',
    expected_value TEXT,
    reasoning TEXT NOT NULL,
    source_quote TEXT NOT NULL DEFAULT '',
    quote_found_in_source INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, target_kind, target_id)
);

CREATE INDEX IF NOT EXISTS idx_review_runs_job
    ON review_runs(extraction_job_id, model, prompt_version);

CREATE INDEX IF NOT EXISTS idx_review_verdicts_target
    ON review_verdicts(task, target_kind, target_id);
