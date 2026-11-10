-- Event Lead Engine: access rules and starting config
--
-- Model:
--   * Row level security is ON for every table and there are no policies, so the public (anon)
--     key cannot read or write any table directly.
--   * The public key can SELECT only from the masked views below.
--   * Everything else goes through SECURITY DEFINER functions that check a token first.
--   * leads_review_full and account_coverage hold names, companies and contact data. They are
--     not granted to the public key. leads_review_full is reachable only through review_queue().

alter table public.events          enable row level security;
alter table public.accounts        enable row level security;
alter table public.leads           enable row level security;
alter table public.reviewers       enable row level security;
alter table public.app_secrets     enable row level security;
alter table public.audit_log       enable row level security;
alter table public.dlq             enable row level security;
alter table public.outreach_events enable row level security;
alter table public.tuning_rules    enable row level security;

-- Tables: nothing for the public roles.
revoke all on all tables in schema public from anon, authenticated;

-- Private views.
revoke all on public.leads_review_full from public, anon, authenticated;
revoke all on public.account_coverage  from public, anon, authenticated;

-- Public, masked views (read only).
grant select on
  public.leads_review,
  public.events_public,
  public.review_stats,
  public.reject_reasons,
  public.pipeline_health,
  public.sdt_metrics,
  public.node_performance,
  public.tuning_flags
to anon, authenticated;

-- Functions: closed by default, then open only the ones the cockpit and n8n call.
revoke execute on all functions in schema public from public, anon, authenticated;

-- Cockpit (reviewer passcode)
grant execute on function public.review_queue(text, text, integer)       to anon, authenticated;
grant execute on function public.review_lead(text, uuid, text, text)     to anon, authenticated;
grant execute on function public.log_source_open(text, uuid)             to anon, authenticated;
grant execute on function public.set_event_state(text, uuid, text)       to anon, authenticated;

-- n8n workflows (ingest_token)
grant execute on function public.ingest_leads(text, jsonb)                          to anon, authenticated;
grant execute on function public.next_approved(text, integer)                       to anon, authenticated;
grant execute on function public.claim_approved(text, integer, integer)            to anon, authenticated;
grant execute on function public.save_enrichment(text, uuid, jsonb, jsonb, text)   to anon, authenticated;
grant execute on function public.save_contact(text, uuid, text, text, text)        to anon, authenticated;
grant execute on function public.log_dlq(text, text, text, jsonb, text, integer)   to anon, authenticated;
grant execute on function public.dlq_replay(text, integer)                         to anon, authenticated;
grant execute on function public.log_outreach_event(text, jsonb)                   to anon, authenticated;

-- Starting thresholds for the tuning view (change them as real send data comes in).
insert into public.tuning_rules (k, v, note) values
  ('bounce_max',    0.05,  'Starting value. Above this, fix the contact data before more sends'),
  ('demote_ratio',  0.5,   'A positive-reply rate below this share of the overall rate proposes a demotion'),
  ('min_contacted', 30,    'Fewest contacted leads before an exit can be judged'),
  ('promote_ratio', 1.5,   'A positive-reply rate above this multiple of the overall rate proposes a promotion'),
  ('spam_max',      0.003, 'Starting value. Above this, stop sending to the exit')
on conflict (k) do nothing;

-- ---------------------------------------------------------------------------
-- Setup after running the four files (do NOT commit the real values):
--
--   -- 1. A token for the n8n workflows
--   insert into public.app_secrets (k, v) values ('ingest_token', '<long random string>');
--
--   -- 2. A reviewer. Store only the sha256 of the passcode.
--   insert into public.reviewers (name, token_hash)
--   values ('<your name>', encode(extensions.digest('<passcode>', 'sha256'), 'hex'));
-- ---------------------------------------------------------------------------
