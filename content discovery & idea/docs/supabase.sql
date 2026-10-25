-- Run once in THIS project's Supabase: Dashboard -> SQL Editor -> paste -> Run.
-- Safe to run again: everything is "if not exists".
-- Tables are prefixed "discovery_" (agent/store.py TABLE_PREFIX). Do not rename them to the
-- unprefixed names: those are another app's tables when the project is shared.

-- URLs already posted ("posted"), already shown in a digest ("digest"), or already shown as a
-- content idea ("idea": written by python -m agent.discovery.run, so ideas do not repeat across days).
create table if not exists discovery_seen (
    kind  text        not null,
    url   text        not null,
    title text        not null default '',
    at    timestamptz not null default now(),
    primary key (kind, url)
);
create index if not exists discovery_seen_kind_at on discovery_seen (kind, at desc);

-- Small JSON blobs by key. "last_run" = the last draft, used by /latest and /repurpose.
create table if not exists discovery_kv (
    key   text        primary key,
    value jsonb       not null,
    at    timestamptz not null default now()
);

-- One row per decision on a draft: saved / rejected / expired.
create table if not exists discovery_feedback (
    id           bigserial   primary key,
    at           timestamptz not null default now(),
    status       text        not null,
    target       text,
    item         jsonb,
    angle        text,
    tweet_before text,
    tweet        text,
    linkedin     text,
    feedback     text
);

-- One row per topic per day: what each discovery run saw of a story (agent/discovery/trends.py reads these to say
-- whether a topic is new, rising or popular). First sighting of the day wins; a re-run the same day adds nothing.
--   origins: the independent sources that carried it (affiliated accounts and reposts already folded together)
--   items:   [{"u": canonical url, "m": {"likes": 900, "views": 98226}}]  engagement per link, for before/after comparison
create table if not exists discovery_topic_obs (
    topic_key    text        not null,
    obs_date     date        not null,
    observed_at  timestamptz not null default now(),
    run_id       text        not null,
    title        text        not null default '',
    origins      jsonb       not null default '[]'::jsonb,
    items        jsonb       not null default '[]'::jsonb,
    claim_status text,
    n_evidence   integer     not null default 0,
    primary key (topic_key, obs_date)
);
create index if not exists discovery_topic_obs_date on discovery_topic_obs (obs_date desc);

-- Row Level Security on, with no policies: the public "anon" key can read and write nothing.
-- The app uses the secret key, which bypasses RLS. Keep that key server-side only.
alter table discovery_seen     enable row level security;
alter table discovery_kv       enable row level security;
alter table discovery_feedback enable row level security;
alter table discovery_topic_obs enable row level security;

-- Make the REST API see the new tables now instead of after its next cache refresh. Without this the
-- first run can still answer "Could not find the table 'public.discovery_seen'" (error PGRST205).
notify pgrst, 'reload schema';

-- Check afterwards: python -m agent.discovery.seen   (should print "Supabase: ok")
