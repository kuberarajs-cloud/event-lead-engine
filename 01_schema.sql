-- Event Lead Engine: tables, constraints and indexes
-- Exported from the live Supabase (Postgres 17) project. Schema only, no data.
-- Run files in order: 01_schema, 02_views, 03_functions, 04_security.

create extension if not exists pgcrypto with schema extensions;
create extension if not exists pg_net with schema extensions;

create table public.events (
  id uuid not null default gen_random_uuid(),
  slug text not null,
  name text not null,
  market text not null,
  city text,
  start_date date,
  end_date date,
  date_label text,
  official_url text not null,
  speakers_url text,
  sponsors_url text,
  list_public text,
  found_via text,
  s_buyer integer,
  s_icp integer,
  s_lead integer,
  s_timing integer,
  s_cost integer,
  -- 50-point rubric: buyer x3, ICP x3, lead data x2, timing x1, cost x1 (each factor 0 to 5)
  score integer generated always as (((((s_buyer * 3) + (s_icp * 3)) + (s_lead * 2)) + s_timing) + s_cost) stored,
  verification text not null default 'unverified',
  pool_state text not null default 'alternate',
  notes text,
  created_at timestamptz not null default now(),
  source_status text,
  constraint events_pkey primary key (id),
  constraint events_slug_key unique (slug),
  constraint events_market_check check (market = any (array['US','UK','DE'])),
  constraint events_pool_state_check check (pool_state = any (array['top10','alternate','excluded']))
);

create table public.reviewers (
  id uuid not null default gen_random_uuid(),
  name text not null,
  token_hash text not null,           -- sha256 hex of the passcode, never the passcode itself
  active boolean not null default true,
  created_at timestamptz not null default now(),
  constraint reviewers_pkey primary key (id),
  constraint reviewers_name_key unique (name)
);

create table public.accounts (
  id uuid not null default gen_random_uuid(),
  event_id uuid not null,
  company text not null,
  domain text,
  role text,
  source_url text,
  created_at timestamptz not null default now(),
  constraint accounts_pkey primary key (id),
  constraint accounts_event_id_company_key unique (event_id, company),
  constraint accounts_event_id_fkey foreign key (event_id) references public.events(id) on delete cascade
);

create table public.leads (
  id uuid not null default gen_random_uuid(),
  event_id uuid not null,
  account_id uuid,
  full_name text not null,
  title text,
  company text not null,
  source_url text not null,
  source_kind text not null default 'speaker',
  tier text not null default 'C',
  score_detail jsonb not null default '{}'::jsonb,
  status text not null default 'pending',
  reject_reason text,
  reviewed_at timestamptz,
  clay_url text,
  enrichment jsonb,
  hubspot_payload jsonb,
  idem_key text not null,             -- md5 of event id, lower-cased name and company
  created_at timestamptz not null default now(),
  data_lineage text,
  enrich_claimed_at timestamptz,
  enrich_attempts integer not null default 0,
  reviewed_by uuid,
  contact_email text,
  contact_email_source text,
  contact_email_status text,
  contact_email_at timestamptz,
  suppressed boolean not null default false,
  suppressed_reason text,
  constraint leads_pkey primary key (id),
  constraint leads_idem_key_key unique (idem_key),
  constraint leads_account_id_fkey foreign key (account_id) references public.accounts(id) on delete set null,
  constraint leads_event_id_fkey foreign key (event_id) references public.events(id) on delete cascade,
  constraint leads_reviewed_by_fkey foreign key (reviewed_by) references public.reviewers(id),
  constraint leads_status_check check (status = any (array['pending','approved','rejected'])),
  constraint leads_tier_check check (tier = any (array['A','B','C','X']))
);

create table public.audit_log (
  id bigint generated always as identity,
  at timestamptz not null default now(),
  actor text not null,
  action text not null,
  entity text,
  entity_id uuid,
  detail jsonb not null default '{}'::jsonb,
  constraint audit_log_pkey primary key (id)
);

create table public.dlq (
  id bigint generated always as identity,
  at timestamptz not null default now(),
  workflow text not null,
  node text,
  payload jsonb,
  error text,
  retries integer not null default 0,
  resolved boolean not null default false,
  status text not null default 'open',
  replay_count integer not null default 0,
  next_retry_at timestamptz not null default now(),
  replay_key text,
  resolved_at timestamptz,
  constraint dlq_pkey primary key (id),
  constraint dlq_status_chk check (status = any (array['open','resolved','dead']))
);

create table public.outreach_events (
  id bigint generated always as identity,
  at timestamptz not null default now(),
  event_time timestamptz,
  provider text not null,
  event_type text not null,
  dedupe_key text not null,
  campaign_id text,
  lead_id uuid,
  email_hash text,
  detail jsonb,
  constraint outreach_events_pkey primary key (id),
  constraint outreach_events_provider_dedupe_key_key unique (provider, dedupe_key),
  constraint outreach_events_lead_id_fkey foreign key (lead_id) references public.leads(id) on delete set null,
  constraint outreach_events_event_type_check check (event_type = any (array['sent','reply','bounce','spam','unsubscribe','positive','negative','neutral'])),
  constraint outreach_events_provider_check check (provider = any (array['smartlead','instantly']))
);

create table public.tuning_rules (
  k text not null,
  v numeric not null,
  note text,
  constraint tuning_rules_pkey primary key (k)
);

-- Shared secrets for the workflow engine (for example the ingest token). Never commit values.
create table public.app_secrets (
  k text not null,
  v text not null,
  constraint app_secrets_pkey primary key (k)
);

create index dlq_due_idx on public.dlq using btree (status, next_retry_at);
create index leads_contact_email_idx on public.leads using btree (lower(contact_email));
create index leads_event_idx on public.leads using btree (event_id);
create index leads_status_idx on public.leads using btree (status);
