CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    email         citext UNIQUE,
    handle        text NOT NULL UNIQUE CHECK (handle ~ '^[a-z0-9][a-z0-9_-]{2,31}$'),
    password_hash text,
    is_guest      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (is_guest OR (email IS NOT NULL AND password_hash IS NOT NULL))
);

CREATE TABLE refresh_tokens (
    id         uuid PRIMARY KEY DEFAULT uuidv7(),
    user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    family_id  uuid NOT NULL,
    token_hash bytea NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    used_at    timestamptz,
    revoked_at timestamptz
);
CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id);
CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (user_id);

CREATE TABLE api_keys (
    id           uuid PRIMARY KEY DEFAULT uuidv7(),
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    prefix       text NOT NULL UNIQUE,
    key_hash     bytea NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX api_keys_user_idx ON api_keys (user_id);

CREATE TABLE systems (
    id             uuid PRIMARY KEY DEFAULT uuidv7(),
    owner_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name           text NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    latest_version integer NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);
CREATE INDEX systems_owner_idx ON systems (owner_id, updated_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE TABLE system_versions (
    system_id  uuid NOT NULL REFERENCES systems(id) ON DELETE CASCADE,
    version    integer NOT NULL CHECK (version > 0),
    spec_yaml  text NOT NULL,
    spec_json  jsonb NOT NULL,
    spec_hash  text NOT NULL,
    checks     jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (system_id, version)
);

CREATE TYPE job_status AS ENUM ('queued', 'running', 'done', 'failed', 'cancelled');

CREATE TABLE analyses (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),
    owner_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    system_id       uuid NOT NULL,
    system_version  integer NOT NULL,
    spec_hash       text NOT NULL,
    parent_id       uuid REFERENCES analyses(id) ON DELETE SET NULL,
    params          jsonb NOT NULL,
    priority        smallint NOT NULL DEFAULT 1 CHECK (priority BETWEEN 0 AND 2),
    status          job_status NOT NULL DEFAULT 'queued',
    cells_total     integer NOT NULL,
    cells_done      integer NOT NULL DEFAULT 0,
    sims_total      integer NOT NULL DEFAULT 0,
    sims_done       integer NOT NULL DEFAULT 0,
    sims_cached     integer NOT NULL DEFAULT 0,
    result          jsonb,
    error           text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    first_result_at timestamptz,
    finished_at     timestamptz,
    FOREIGN KEY (system_id, system_version) REFERENCES system_versions (system_id, version) ON DELETE CASCADE
);
CREATE INDEX analyses_owner_idx ON analyses (owner_id, created_at DESC, id DESC);
CREATE INDEX analyses_system_idx ON analyses (system_id, created_at DESC, id DESC);
CREATE INDEX analyses_active_idx ON analyses (owner_id) WHERE status IN ('queued', 'running');

CREATE TABLE analysis_tasks (
    analysis_id   uuid NOT NULL REFERENCES analyses(id) ON DELETE CASCADE,
    cell          integer NOT NULL,
    seed_index    smallint NOT NULL,
    task_hash     text NOT NULL,
    status        text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'dispatched', 'done', 'failed')),
    cached        boolean NOT NULL DEFAULT false,
    summary       jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    finished_at   timestamptz,
    PRIMARY KEY (analysis_id, cell, seed_index)
);

CREATE TABLE sim_results (
    task_hash      text PRIMARY KEY,
    engine_version text NOT NULL,
    summary        jsonb NOT NULL,
    events         bigint NOT NULL,
    wall_ms        real NOT NULL,
    hits           integer NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_hit_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sim_results_lru_idx ON sim_results (last_hit_at);

CREATE TABLE runs (
    id             uuid PRIMARY KEY DEFAULT uuidv7(),
    owner_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    system_id      uuid NOT NULL,
    system_version integer NOT NULL,
    spec_hash      text NOT NULL,
    params         jsonb NOT NULL,
    status         job_status NOT NULL DEFAULT 'queued',
    worker         text,
    lease_token    bigint NOT NULL DEFAULT 0,
    sim_time       double precision NOT NULL DEFAULT 0,
    summary        jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    ended_at       timestamptz,
    FOREIGN KEY (system_id, system_version) REFERENCES system_versions (system_id, version) ON DELETE CASCADE
);
CREATE INDEX runs_owner_idx ON runs (owner_id, created_at DESC, id DESC);
CREATE INDEX runs_active_idx ON runs (status) WHERE status IN ('queued', 'running');

CREATE TABLE run_commands (
    run_id     uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq        integer NOT NULL,
    sim_time   double precision NOT NULL,
    command    jsonb NOT NULL,
    issued_by  uuid REFERENCES users(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE reports (
    slug        text PRIMARY KEY,
    analysis_id uuid NOT NULL UNIQUE REFERENCES analyses(id) ON DELETE CASCADE,
    snapshot    jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE idempotency_keys (
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key          text NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
    request_hash text NOT NULL,
    status_code  smallint,
    response     jsonb,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, key)
);
CREATE INDEX idempotency_keys_created_idx ON idempotency_keys (created_at);

CREATE TABLE outbox (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    topic        text NOT NULL,
    payload      jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX outbox_pending_idx ON outbox (id) WHERE published_at IS NULL;

CREATE TABLE activity_log (
    id         bigint GENERATED ALWAYS AS IDENTITY,
    user_id    uuid,
    kind       text NOT NULL,
    data       jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);
CREATE TABLE activity_log_default PARTITION OF activity_log DEFAULT;
CREATE INDEX activity_log_kind_idx ON activity_log (kind, created_at);

CREATE FUNCTION ensure_activity_partitions(ahead integer DEFAULT 2) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    m date := date_trunc('month', now())::date;
    i integer;
    part text;
BEGIN
    FOR i IN 0..ahead LOOP
        part := format('activity_log_%s', to_char(m + make_interval(months => i), 'YYYY_MM'));
        IF to_regclass(part) IS NULL THEN
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF activity_log FOR VALUES FROM (%L) TO (%L)',
                part, m + make_interval(months => i), m + make_interval(months => i + 1));
        END IF;
    END LOOP;
END $$;

SELECT ensure_activity_partitions();
