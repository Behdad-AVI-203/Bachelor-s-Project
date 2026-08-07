PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER PRIMARY KEY,
    description     TEXT NOT NULL,
    applied_at      TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS code_artifacts (
    id                  INTEGER PRIMARY KEY,
    artifact_type       TEXT NOT NULL CHECK (
        artifact_type IN (
            'reward_function',
            'blockchain_logic',
            'profit_expectation'
        )
    ),
    name                TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    original_filename   TEXT,
    entrypoint          TEXT NOT NULL,
    source_code         TEXT NOT NULL,
    sha256              TEXT NOT NULL CHECK (length(sha256) = 64),
    validation_status   TEXT NOT NULL DEFAULT 'pending' CHECK (
        validation_status IN ('pending', 'valid', 'invalid')
    ),
    validation_message  TEXT,
    created_at          TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE (artifact_type, name, version)
);

CREATE TABLE IF NOT EXISTS iot_environments (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    description     TEXT,
    revision        INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    metadata_json   TEXT NOT NULL DEFAULT '{}'
                    CHECK (json_valid(metadata_json)),
    created_at      TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at      TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS device_behaviors (
    id                              INTEGER PRIMARY KEY,
    name                            TEXT NOT NULL,
    version                         INTEGER NOT NULL DEFAULT 1,
    precision                       REAL NOT NULL CHECK (precision >= 0),
    execution_cost                  REAL NOT NULL CHECK (execution_cost >= 0),
    data_rate                       REAL NOT NULL CHECK (data_rate >= 0),
    profit_expectation              REAL NOT NULL DEFAULT 0
                                    CHECK (profit_expectation >= 0),
    profit_expectation_artifact_id  INTEGER,
    parameters_json                 TEXT NOT NULL DEFAULT '{}'
                                    CHECK (json_valid(parameters_json)),
    created_at                      TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (profit_expectation_artifact_id)
        REFERENCES code_artifacts(id)
        ON DELETE SET NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS environment_devices (
    id                  INTEGER PRIMARY KEY,
    environment_id      INTEGER NOT NULL,
    behavior_id         INTEGER NOT NULL,
    device_key          TEXT NOT NULL,
    display_name        TEXT,
    initial_balance     REAL NOT NULL DEFAULT 0,
    enabled             INTEGER NOT NULL DEFAULT 1
                        CHECK (enabled IN (0, 1)),
    overrides_json      TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(overrides_json)),
    metadata_json       TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(metadata_json)),
    created_at          TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (environment_id)
        REFERENCES iot_environments(id)
        ON DELETE CASCADE,
    FOREIGN KEY (behavior_id)
        REFERENCES device_behaviors(id)
        ON DELETE RESTRICT,
    UNIQUE (environment_id, device_key)
);

CREATE TABLE IF NOT EXISTS blockchain_network_configs (
    id                              INTEGER PRIMARY KEY,
    name                            TEXT NOT NULL,
    version                         INTEGER NOT NULL DEFAULT 1,
    description                     TEXT,
    consensus_type                  TEXT NOT NULL DEFAULT 'pow',
    pow_difficulty                  INTEGER NOT NULL DEFAULT 2
                                    CHECK (pow_difficulty >= 0),
    max_transactions_per_block      INTEGER NOT NULL DEFAULT 30
                                    CHECK (max_transactions_per_block > 0),
    target_block_time_ms            INTEGER CHECK (
                                        target_block_time_ms IS NULL
                                        OR target_block_time_ms > 0
                                    ),
    transaction_fee_rate            REAL NOT NULL DEFAULT 0
                                    CHECK (transaction_fee_rate >= 0),
    reward_artifact_id              INTEGER,
    blockchain_logic_artifact_id    INTEGER,
    parameters_json                 TEXT NOT NULL DEFAULT '{}'
                                    CHECK (json_valid(parameters_json)),
    created_at                      TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (reward_artifact_id)
        REFERENCES code_artifacts(id)
        ON DELETE SET NULL,
    FOREIGN KEY (blockchain_logic_artifact_id)
        REFERENCES code_artifacts(id)
        ON DELETE SET NULL,
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS experiment_configs (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    description             TEXT,
    environment_id          INTEGER NOT NULL,
    network_a_config_id     INTEGER NOT NULL,
    network_b_config_id     INTEGER NOT NULL,
    poisson_lambda          REAL NOT NULL CHECK (poisson_lambda > 0),
    duration_seconds        REAL NOT NULL CHECK (duration_seconds > 0),
    sample_interval_ms      INTEGER NOT NULL CHECK (sample_interval_ms > 0),
    default_random_seed     INTEGER,
    traffic_mix_json        TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(traffic_mix_json)),
    parameters_json         TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(parameters_json)),
    created_at              TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at              TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (environment_id)
        REFERENCES iot_environments(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (network_a_config_id)
        REFERENCES blockchain_network_configs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (network_b_config_id)
        REFERENCES blockchain_network_configs(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS configuration_bundles (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    bundle_type         TEXT NOT NULL CHECK (
        bundle_type IN ('environment', 'network', 'experiment', 'full')
    ),
    schema_version      INTEGER NOT NULL CHECK (schema_version > 0),
    payload_json        TEXT NOT NULL CHECK (json_valid(payload_json)),
    sha256              TEXT NOT NULL CHECK (length(sha256) = 64),
    source              TEXT NOT NULL DEFAULT 'export' CHECK (
        source IN ('export', 'import', 'run_snapshot')
    ),
    created_at          TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE IF NOT EXISTS simulation_runs (
    id                          INTEGER PRIMARY KEY,
    experiment_config_id        INTEGER,
    environment_id              INTEGER NOT NULL,
    name                        TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'created' CHECK (
        status IN (
            'created',
            'queued',
            'running',
            'completed',
            'failed',
            'cancelled'
        )
    ),
    poisson_lambda              REAL NOT NULL CHECK (poisson_lambda > 0),
    duration_seconds            REAL NOT NULL CHECK (duration_seconds > 0),
    sample_interval_ms          INTEGER NOT NULL CHECK (sample_interval_ms > 0),
    random_seed                 INTEGER NOT NULL,
    configuration_snapshot_json TEXT NOT NULL
                                CHECK (json_valid(configuration_snapshot_json)),
    started_at                  TEXT,
    completed_at                TEXT,
    error_message               TEXT,
    created_at                  TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (experiment_config_id)
        REFERENCES experiment_configs(id)
        ON DELETE SET NULL,
    FOREIGN KEY (environment_id)
        REFERENCES iot_environments(id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS simulation_networks (
    id                          INTEGER PRIMARY KEY,
    simulation_id               INTEGER NOT NULL,
    network_slot                TEXT NOT NULL CHECK (network_slot IN ('A', 'B')),
    network_config_id           INTEGER NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'created' CHECK (
        status IN (
            'created',
            'running',
            'completed',
            'failed',
            'cancelled'
        )
    ),
    configuration_snapshot_json TEXT NOT NULL
                                CHECK (json_valid(configuration_snapshot_json)),
    started_at                  TEXT,
    completed_at                TEXT,
    error_message               TEXT,
    FOREIGN KEY (simulation_id)
        REFERENCES simulation_runs(id)
        ON DELETE CASCADE,
    FOREIGN KEY (network_config_id)
        REFERENCES blockchain_network_configs(id)
        ON DELETE RESTRICT,
    UNIQUE (simulation_id, network_slot)
);

CREATE TABLE IF NOT EXISTS simulation_devices (
    id                      INTEGER PRIMARY KEY,
    simulation_id           INTEGER NOT NULL,
    source_device_id        INTEGER,
    device_key              TEXT NOT NULL,
    display_name            TEXT,
    initial_balance         REAL NOT NULL,
    precision               REAL NOT NULL,
    execution_cost          REAL NOT NULL,
    data_rate               REAL NOT NULL,
    profit_expectation      REAL NOT NULL,
    behavior_snapshot_json  TEXT NOT NULL
                            CHECK (json_valid(behavior_snapshot_json)),
    FOREIGN KEY (simulation_id)
        REFERENCES simulation_runs(id)
        ON DELETE CASCADE,
    FOREIGN KEY (source_device_id)
        REFERENCES environment_devices(id)
        ON DELETE SET NULL,
    UNIQUE (simulation_id, device_key)
);

CREATE TABLE IF NOT EXISTS simulation_events (
    id                  INTEGER PRIMARY KEY,
    simulation_id       INTEGER NOT NULL,
    sequence_number     INTEGER NOT NULL CHECK (sequence_number >= 0),
    scheduled_at_ms     INTEGER NOT NULL CHECK (scheduled_at_ms >= 0),
    event_type          TEXT NOT NULL CHECK (
        event_type IN ('transfer', 'iot_data', 'feedback')
    ),
    sender_device_id    INTEGER,
    target_device_id    INTEGER,
    amount              REAL,
    payload_json        TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(payload_json)),
    FOREIGN KEY (simulation_id)
        REFERENCES simulation_runs(id)
        ON DELETE CASCADE,
    FOREIGN KEY (sender_device_id)
        REFERENCES simulation_devices(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (target_device_id)
        REFERENCES simulation_devices(id)
        ON DELETE RESTRICT,
    UNIQUE (simulation_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS blocks (
    id                      INTEGER PRIMARY KEY,
    simulation_network_id   INTEGER NOT NULL,
    height                  INTEGER NOT NULL CHECK (height >= 0),
    block_hash              TEXT NOT NULL,
    previous_hash           TEXT,
    mined_at_ms             INTEGER NOT NULL CHECK (mined_at_ms >= 0),
    nonce                   INTEGER,
    difficulty              INTEGER NOT NULL CHECK (difficulty >= 0),
    miner_address           TEXT,
    transaction_count       INTEGER NOT NULL DEFAULT 0,
    total_fees              REAL NOT NULL DEFAULT 0,
    total_block_reward      REAL NOT NULL DEFAULT 0,
    mining_duration_ms      REAL,
    metadata_json           TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(metadata_json)),
    FOREIGN KEY (simulation_network_id)
        REFERENCES simulation_networks(id)
        ON DELETE CASCADE,
    UNIQUE (simulation_network_id, height),
    UNIQUE (simulation_network_id, block_hash)
);

CREATE TABLE IF NOT EXISTS network_transactions (
    id                      INTEGER PRIMARY KEY,
    simulation_network_id   INTEGER NOT NULL,
    event_id                INTEGER,
    block_id                INTEGER,
    transaction_hash        TEXT,
    transaction_type        TEXT NOT NULL CHECK (
        transaction_type IN (
            'transfer',
            'iot_data',
            'feedback',
            'block_reward',
            'system'
        )
    ),
    status                  TEXT NOT NULL CHECK (
        status IN (
            'generated',
            'accepted',
            'pending',
            'confirmed',
            'rejected',
            'failed'
        )
    ),
    sender_device_id        INTEGER,
    target_device_id        INTEGER,
    sender_address          TEXT,
    target_address          TEXT,
    submitted_at_ms         INTEGER NOT NULL CHECK (submitted_at_ms >= 0),
    confirmed_at_ms         INTEGER,
    amount                  REAL NOT NULL DEFAULT 0,
    fee                     REAL NOT NULL DEFAULT 0,
    reward                  REAL NOT NULL DEFAULT 0,
    payload_json            TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(payload_json)),
    rejection_reason        TEXT,
    FOREIGN KEY (simulation_network_id)
        REFERENCES simulation_networks(id)
        ON DELETE CASCADE,
    FOREIGN KEY (event_id)
        REFERENCES simulation_events(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (block_id)
        REFERENCES blocks(id)
        ON DELETE SET NULL,
    FOREIGN KEY (sender_device_id)
        REFERENCES simulation_devices(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (target_device_id)
        REFERENCES simulation_devices(id)
        ON DELETE RESTRICT,
    UNIQUE (simulation_network_id, event_id)
);

CREATE TABLE IF NOT EXISTS device_state_samples (
    simulation_network_id   INTEGER NOT NULL,
    simulation_device_id    INTEGER NOT NULL,
    elapsed_ms              INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    balance                 REAL NOT NULL,
    cumulative_reward       REAL NOT NULL DEFAULT 0,
    cumulative_cost         REAL NOT NULL DEFAULT 0,
    cumulative_profit       REAL NOT NULL DEFAULT 0,
    feedback_score          REAL NOT NULL DEFAULT 0,
    submitted_transactions  INTEGER NOT NULL DEFAULT 0,
    confirmed_transactions  INTEGER NOT NULL DEFAULT 0,
    rejected_transactions   INTEGER NOT NULL DEFAULT 0,
    is_active               INTEGER NOT NULL CHECK (is_active IN (0, 1)),
    churn_reason            TEXT,
    extra_state_json        TEXT NOT NULL DEFAULT '{}'
                            CHECK (json_valid(extra_state_json)),
    PRIMARY KEY (
        simulation_network_id,
        simulation_device_id,
        elapsed_ms
    ),
    FOREIGN KEY (simulation_network_id)
        REFERENCES simulation_networks(id)
        ON DELETE CASCADE,
    FOREIGN KEY (simulation_device_id)
        REFERENCES simulation_devices(id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS network_metric_samples (
    simulation_network_id       INTEGER NOT NULL,
    elapsed_ms                  INTEGER NOT NULL CHECK (elapsed_ms >= 0),
    active_device_count         INTEGER NOT NULL,
    churned_device_count        INTEGER NOT NULL,
    churn_rate                  REAL NOT NULL,
    gini_coefficient            REAL,
    balance_variance            REAL,
    generated_transaction_count INTEGER NOT NULL DEFAULT 0,
    accepted_transaction_count  INTEGER NOT NULL DEFAULT 0,
    confirmed_transaction_count INTEGER NOT NULL DEFAULT 0,
    rejected_transaction_count  INTEGER NOT NULL DEFAULT 0,
    pending_transaction_count   INTEGER NOT NULL DEFAULT 0,
    block_count                 INTEGER NOT NULL DEFAULT 0,
    throughput_tps              REAL NOT NULL DEFAULT 0,
    average_confirmation_ms     REAL,
    total_rewards               REAL NOT NULL DEFAULT 0,
    total_fees                  REAL NOT NULL DEFAULT 0,
    custom_metrics_json         TEXT NOT NULL DEFAULT '{}'
                                CHECK (json_valid(custom_metrics_json)),
    PRIMARY KEY (simulation_network_id, elapsed_ms),
    FOREIGN KEY (simulation_network_id)
        REFERENCES simulation_networks(id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS network_run_summaries (
    simulation_network_id       INTEGER PRIMARY KEY,
    final_active_devices        INTEGER NOT NULL,
    final_churn_rate            REAL NOT NULL,
    final_gini_coefficient      REAL,
    final_balance_variance      REAL,
    total_transactions          INTEGER NOT NULL,
    confirmed_transactions      INTEGER NOT NULL,
    rejected_transactions       INTEGER NOT NULL,
    total_blocks                INTEGER NOT NULL,
    average_throughput_tps      REAL,
    average_confirmation_ms     REAL,
    total_rewards               REAL NOT NULL DEFAULT 0,
    total_costs                 REAL NOT NULL DEFAULT 0,
    custom_summary_json         TEXT NOT NULL DEFAULT '{}'
                                CHECK (json_valid(custom_summary_json)),
    calculated_at               TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (simulation_network_id)
        REFERENCES simulation_networks(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS simulation_comparisons (
    simulation_id       INTEGER PRIMARY KEY,
    score_a             REAL,
    score_b             REAL,
    winner_slot         TEXT CHECK (
        winner_slot IS NULL OR winner_slot IN ('A', 'B', 'TIE')
    ),
    summary_json        TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(summary_json)),
    calculated_at       TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (simulation_id)
        REFERENCES simulation_runs(id)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS comparison_metrics (
    simulation_id       INTEGER NOT NULL,
    metric_name         TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    unit                TEXT,
    value_a             REAL,
    value_b             REAL,
    absolute_delta      REAL,
    relative_delta      REAL,
    preferred_direction TEXT NOT NULL DEFAULT 'neutral' CHECK (
        preferred_direction IN ('higher', 'lower', 'neutral')
    ),
    winner_slot         TEXT CHECK (
        winner_slot IS NULL OR winner_slot IN ('A', 'B', 'TIE')
    ),
    details_json        TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(details_json)),
    PRIMARY KEY (simulation_id, metric_name),
    FOREIGN KEY (simulation_id)
        REFERENCES simulation_runs(id)
        ON DELETE CASCADE
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_devices_environment
    ON environment_devices(environment_id);

CREATE INDEX IF NOT EXISTS idx_devices_behavior
    ON environment_devices(behavior_id);

CREATE INDEX IF NOT EXISTS idx_runs_created
    ON simulation_runs(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_runs_status
    ON simulation_runs(status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_simulation_networks_run
    ON simulation_networks(simulation_id, network_slot);

CREATE INDEX IF NOT EXISTS idx_simulation_devices_run
    ON simulation_devices(simulation_id);

CREATE INDEX IF NOT EXISTS idx_events_timeline
    ON simulation_events(
        simulation_id,
        scheduled_at_ms,
        sequence_number
    );

CREATE INDEX IF NOT EXISTS idx_events_type
    ON simulation_events(simulation_id, event_type);

CREATE INDEX IF NOT EXISTS idx_blocks_timeline
    ON blocks(simulation_network_id, mined_at_ms);

CREATE INDEX IF NOT EXISTS idx_transactions_timeline
    ON network_transactions(simulation_network_id, submitted_at_ms);

CREATE INDEX IF NOT EXISTS idx_transactions_status_type
    ON network_transactions(
        simulation_network_id,
        status,
        transaction_type
    );

CREATE INDEX IF NOT EXISTS idx_transactions_block
    ON network_transactions(block_id);

CREATE INDEX IF NOT EXISTS idx_device_states_timeline
    ON device_state_samples(simulation_network_id, elapsed_ms);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'Initial simulation platform schema');
