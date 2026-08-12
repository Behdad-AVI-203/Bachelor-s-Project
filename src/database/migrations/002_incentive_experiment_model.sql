CREATE TABLE code_artifacts_v2 (
    id                  INTEGER PRIMARY KEY,
    artifact_type       TEXT NOT NULL CHECK (
        artifact_type IN (
            'reward_function',
            'incentive_mechanism',
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

INSERT INTO code_artifacts_v2
SELECT * FROM code_artifacts;

DROP TABLE code_artifacts;
ALTER TABLE code_artifacts_v2 RENAME TO code_artifacts;

CREATE TABLE incentive_mechanism_configs (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    description         TEXT,
    implementation_type TEXT NOT NULL CHECK (
        implementation_type IN ('built_in', 'custom', 'legacy_reward')
    ),
    built_in_key        TEXT,
    code_artifact_id    INTEGER,
    parameters_json     TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(parameters_json)),
    metadata_json       TEXT NOT NULL DEFAULT '{}'
                        CHECK (json_valid(metadata_json)),
    created_at          TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    FOREIGN KEY (code_artifact_id)
        REFERENCES code_artifacts(id)
        ON DELETE RESTRICT,
    CHECK (
        (
            implementation_type = 'built_in'
            AND built_in_key IS NOT NULL
            AND length(trim(built_in_key)) > 0
        )
        OR (
            implementation_type IN ('custom', 'legacy_reward')
            AND code_artifact_id IS NOT NULL
        )
    ),
    UNIQUE (name, version)
);

CREATE TABLE experiment_configs_v2 (
    id                      INTEGER PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
    description             TEXT,
    environment_id          INTEGER NOT NULL,
    comparison_model        TEXT NOT NULL DEFAULT 'legacy_networks' CHECK (
                                comparison_model IN (
                                    'legacy_networks',
                                    'incentive_mechanisms'
                                )
                            ),
    network_a_config_id     INTEGER,
    network_b_config_id     INTEGER,
    network_config_id       INTEGER,
    incentive_a_config_id   INTEGER,
    incentive_b_config_id   INTEGER,
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
        ON DELETE RESTRICT,
    FOREIGN KEY (network_config_id)
        REFERENCES blockchain_network_configs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (incentive_a_config_id)
        REFERENCES incentive_mechanism_configs(id)
        ON DELETE RESTRICT,
    FOREIGN KEY (incentive_b_config_id)
        REFERENCES incentive_mechanism_configs(id)
        ON DELETE RESTRICT,
    CHECK (
        (
            comparison_model = 'legacy_networks'
            AND network_a_config_id IS NOT NULL
            AND network_b_config_id IS NOT NULL
            AND network_config_id IS NULL
            AND incentive_a_config_id IS NULL
            AND incentive_b_config_id IS NULL
        )
        OR (
            comparison_model = 'incentive_mechanisms'
            AND network_a_config_id IS NULL
            AND network_b_config_id IS NULL
            AND network_config_id IS NOT NULL
            AND incentive_a_config_id IS NOT NULL
            AND incentive_b_config_id IS NOT NULL
        )
    )
);

INSERT INTO experiment_configs_v2 (
    id,
    name,
    description,
    environment_id,
    comparison_model,
    network_a_config_id,
    network_b_config_id,
    poisson_lambda,
    duration_seconds,
    sample_interval_ms,
    default_random_seed,
    traffic_mix_json,
    parameters_json,
    created_at,
    updated_at
)
SELECT
    id,
    name,
    description,
    environment_id,
    'legacy_networks',
    network_a_config_id,
    network_b_config_id,
    poisson_lambda,
    duration_seconds,
    sample_interval_ms,
    default_random_seed,
    traffic_mix_json,
    parameters_json,
    created_at,
    updated_at
FROM experiment_configs;

DROP TABLE experiment_configs;
ALTER TABLE experiment_configs_v2 RENAME TO experiment_configs;

CREATE TABLE configuration_bundles_v2 (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    bundle_type         TEXT NOT NULL CHECK (
        bundle_type IN (
            'environment',
            'network',
            'incentive',
            'experiment',
            'full'
        )
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

INSERT INTO configuration_bundles_v2
SELECT * FROM configuration_bundles;

DROP TABLE configuration_bundles;
ALTER TABLE configuration_bundles_v2 RENAME TO configuration_bundles;

CREATE INDEX idx_incentive_configs_artifact
    ON incentive_mechanism_configs(code_artifact_id);

CREATE INDEX idx_experiments_network
    ON experiment_configs(network_config_id);

CREATE INDEX idx_experiments_incentives
    ON experiment_configs(incentive_a_config_id, incentive_b_config_id);
