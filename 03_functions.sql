-- Event Lead Engine: functions (RPC layer), trigger
-- All are SECURITY DEFINER with a fixed search_path. Every write path checks a token first.
--   reviewer passcode  -> checked against reviewers.token_hash (sha256), used by the cockpit
--   workflow token     -> checked against app_secrets (key 'ingest_token'), used by n8n

-- Token checks -------------------------------------------------------------

create or replace function public._reviewer(p_token text)
 returns uuid
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare rid uuid;
begin
  select id into rid from reviewers where active and p_token is not null and token_hash = encode(extensions.digest(p_token, 'sha256'), 'hex');
  if rid is null then raise exception 'unauthorized'; end if;
  return rid;
end $function$;

create or replace function public._chk(p_key text, p_token text)
 returns void
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin
  if p_key = 'review_token' then perform public._reviewer(p_token); return; end if;
  if p_token is null or not exists (select 1 from app_secrets where k = p_key and v = p_token) then raise exception 'unauthorized'; end if;
end $function$;

-- Cockpit: review ----------------------------------------------------------

create or replace function public.review_queue(p_token text, p_status text default null::text, p_limit integer default 500)
 returns setof leads_review
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin
  perform public._reviewer(p_token);
  return query
    select * from public.leads_review_full
    where (p_status is null or status = p_status)
    order by case tier when 'A' then 1 when 'B' then 2 when 'C' then 3 else 4 end, created_at desc
    limit p_limit;
end
$function$;

create or replace function public.review_lead(p_token text, p_lead uuid, p_decision text, p_reason text default null::text)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare r leads; rid uuid; rname text;
begin
  rid := public._reviewer(p_token);
  select name into rname from reviewers where id = rid;
  if p_decision not in ('approved', 'rejected', 'pending') then raise exception 'bad decision'; end if;
  if p_decision = 'rejected' and coalesce(trim(p_reason), '') = '' then raise exception 'reason required'; end if;
  update leads set status = p_decision, reject_reason = case when p_decision = 'rejected' then trim(p_reason) else null end, reviewed_at = case when p_decision = 'pending' then null else now() end, reviewed_by = case when p_decision = 'pending' then null else rid end where id = p_lead returning * into r;
  if not found then raise exception 'lead not found'; end if;
  insert into audit_log(actor, action, entity, entity_id, detail) values (rname, 'review_lead', 'lead', p_lead, jsonb_build_object('decision', p_decision, 'reason', p_reason));
  return jsonb_build_object('id', r.id, 'status', r.status);
end $function$;

create or replace function public.log_source_open(p_token text, p_lead uuid)
 returns void
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare rname text;
begin
  select name into rname from reviewers where id = public._reviewer(p_token);
  insert into audit_log(actor, action, entity, entity_id) values (rname, 'source_open', 'lead', p_lead);
end $function$;

create or replace function public.set_event_state(p_token text, p_event uuid, p_state text)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare rname text;
begin
  select name into rname from reviewers where id = public._reviewer(p_token);
  if p_state not in ('top10', 'alternate', 'excluded') then raise exception 'bad state'; end if;
  update events set pool_state = p_state where id = p_event;
  if not found then raise exception 'event not found'; end if;
  insert into audit_log(actor, action, entity, entity_id, detail) values (rname, 'set_event_state', 'event', p_event, jsonb_build_object('state', p_state));
  return jsonb_build_object('id', p_event, 'state', p_state);
end $function$;

-- Workflows: sourcing ------------------------------------------------------

create or replace function public.ingest_leads(p_token text, p_rows jsonb)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare x jsonb; ev uuid; acc uuid; k text; ins int := 0; upd int := 0; skp int := 0; was_insert boolean; slugs text[] := '{}';
begin
  perform _chk('ingest_token', p_token);
  for x in select * from jsonb_array_elements(p_rows) loop
    select id into ev from events where slug = x->>'event_slug';
    if ev is null or coalesce(trim(x->>'full_name'), '') = '' or coalesce(trim(x->>'company'), '') = '' then skp := skp + 1; continue; end if;
    slugs := slugs || (x->>'event_slug');
    insert into accounts(event_id, company, role, source_url) values (ev, trim(x->>'company'), x->>'source_kind', x->>'source_url') on conflict (event_id, company) do update set source_url = excluded.source_url returning id into acc;
    k := md5(ev::text || '|' || lower(trim(x->>'full_name')) || '|' || lower(trim(x->>'company')));
    was_insert := null;
    insert into leads(event_id, account_id, full_name, title, company, source_url, source_kind, data_lineage, tier, score_detail, idem_key)
    values (ev, acc, trim(x->>'full_name'), x->>'title', trim(x->>'company'), x->>'source_url', coalesce(x->>'source_kind', 'speaker'),
      coalesce(x->>'data_lineage', case when coalesce(x->>'source_kind', '') ilike 'linkedin%' then 'Google result for a public LinkedIn post (unverified mention). Read by n8n through the Serper API.' else 'Organiser speaker page (public list). Read by n8n over HTTP after a robots.txt check.' end),
      coalesce(x->>'tier', 'C'), coalesce(x->'score_detail', '{}'::jsonb), k)
    on conflict (idem_key) do update set tier = excluded.tier, score_detail = excluded.score_detail, title = excluded.title
      where leads.status = 'pending' and (leads.tier, leads.score_detail, leads.title) is distinct from (excluded.tier, excluded.score_detail, excluded.title)
    returning (xmax = 0) into was_insert;
    if was_insert is null then skp := skp + 1; elsif was_insert then ins := ins + 1; else upd := upd + 1; end if;
  end loop;
  update dlq set status = 'resolved', resolved = true, resolved_at = now() where status = 'open' and workflow = 'sourcing' and replay_key = any(slugs);
  insert into audit_log(actor, action, entity, detail) values ('n8n', 'ingest_leads', 'batch', jsonb_build_object('inserted', ins, 'rescored', upd, 'skipped', skp));
  return jsonb_build_object('inserted', ins, 'rescored', upd, 'skipped', skp);
end $function$;

-- Workflows: enrichment ----------------------------------------------------

create or replace function public.next_approved(p_token text, p_limit integer default 20)
 returns table(id uuid, full_name text, title text, company text, event_slug text, event_name text, source_url text, source_kind text, tier text, score_detail jsonb)
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin
  perform _chk('ingest_token', p_token);
  return query
    select l.id, l.full_name, l.title, l.company, e.slug, e.name, l.source_url, l.source_kind, l.tier, l.score_detail
    from leads l join events e on e.id = l.event_id
    where l.status = 'approved' and l.enrichment is null
    order by l.reviewed_at limit p_limit;
end $function$;

-- Claims approved leads for enrichment. SKIP LOCKED lets two workers run without taking the same lead;
-- a claim older than p_stale_minutes is released, and a lead is tried at most 5 times.
create or replace function public.claim_approved(p_token text, p_limit integer default 20, p_stale_minutes integer default 10)
 returns table(id uuid, full_name text, title text, company text, event_slug text, event_name text, source_url text, source_kind text, tier text, score_detail jsonb)
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin perform _chk('ingest_token', p_token);
return query with picked as (
select l.id as lid from leads l where l.status = 'approved' and l.enrichment is null and l.enrich_attempts < 5 and (l.enrich_claimed_at is null or l.enrich_claimed_at < now() - make_interval(mins => p_stale_minutes))
order by l.tier, l.reviewed_at limit p_limit for update skip locked
), upd as (
update leads l set enrich_claimed_at = now(), enrich_attempts = l.enrich_attempts + 1 from picked where l.id = picked.lid returning l.id, l.full_name, l.title, l.company, l.event_id, l.source_url, l.source_kind, l.tier, l.score_detail, l.reviewed_at
) select u.id, u.full_name, u.title, u.company, e.slug, e.name, u.source_url, u.source_kind, u.tier, u.score_detail from upd u join events e on e.id = u.event_id order by u.tier, u.reviewed_at;
end $function$;

create or replace function public.save_enrichment(p_token text, p_lead uuid, p_enrichment jsonb, p_hubspot jsonb, p_clay_url text default null::text)
 returns void
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin
  perform _chk('ingest_token', p_token);
  update leads set enrichment = p_enrichment, hubspot_payload = p_hubspot, clay_url = coalesce(p_clay_url, clay_url)
   where id = p_lead and status = 'approved';
  insert into audit_log(actor, action, entity, entity_id) values ('n8n', 'save_enrichment', 'lead', p_lead);
end $function$;

create or replace function public.save_contact(p_token text, p_lead uuid, p_email text, p_source text, p_status text)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin perform _chk('ingest_token', p_token);
if p_status not in ('valid','risky','invalid','unknown') then raise exception 'bad status'; end if;
update leads set contact_email = lower(trim(p_email)), contact_email_source = p_source, contact_email_status = p_status, contact_email_at = now() where id = p_lead;
return jsonb_build_object('updated', found); end $function$;

-- Workflows: dead-letter queue ---------------------------------------------

create or replace function public.log_dlq(p_token text, p_workflow text, p_node text, p_payload jsonb, p_error text, p_retries integer default 0)
 returns void
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
begin
  perform _chk('ingest_token', p_token);
  insert into dlq(workflow, node, payload, error, retries, replay_key, next_retry_at)
  values (p_workflow, p_node, p_payload, p_error, p_retries, coalesce(p_payload->>'lead_id', p_payload->>'slug', p_payload->'item'->>'lead_id'), now() + interval '5 minutes');
end $function$;

-- Heals rows whose work has since succeeded, then retries due rows with exponential backoff
-- (5, 10, 20 ... capped at 240 minutes, plus jitter). After 5 replays a row is marked 'dead' for a human.
create or replace function public.dlq_replay(p_token text, p_limit integer default 20)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare healed int; items jsonb;
begin
  perform _chk('ingest_token', p_token);
  with h as (
    update dlq d set status = 'resolved', resolved = true, resolved_at = now()
    where d.status = 'open' and (
      (d.replay_key ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' and exists (select 1 from leads l where l.id = d.replay_key::uuid and l.enrichment is not null))
      or (d.workflow = 'sourcing' and exists (select 1 from leads l join events e on e.id = l.event_id where e.slug = d.replay_key and l.created_at > d.at))
    ) returning 1
  ) select count(*) into healed from h;
  with due as (
    select d.id from dlq d where d.status = 'open' and d.next_retry_at <= now() order by d.at limit p_limit for update skip locked
  ), upd as (
    update dlq d set replay_count = d.replay_count + 1, retries = d.replay_count + 1,
      next_retry_at = now() + make_interval(mins => least(240, 5 * (2 ^ d.replay_count)::int)) + make_interval(secs => (random() * 120)::int),
      status = case when d.replay_count + 1 >= 5 then 'dead' else 'open' end
    from due where d.id = due.id returning d.id, d.replay_key, d.replay_count, d.status
  ), rel as (
    update leads l set enrich_claimed_at = null, enrich_attempts = 0
    from upd u where u.status = 'open' and u.replay_key ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' and l.id = u.replay_key::uuid and l.enrichment is null returning l.id
  )
  select coalesce(jsonb_agg(jsonb_build_object('dlq_id', u.id, 'key', u.replay_key, 'attempt', u.replay_count,
    'action', case when u.status = 'dead' then 'needs_human' when u.replay_key ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' then 'lead_released_for_enrichment' else 'source_rerun_due' end)), '[]'::jsonb) into items from upd u;
  if healed > 0 or jsonb_array_length(items) > 0 then
    insert into audit_log(actor, action, entity, detail) values ('n8n', 'dlq_replay', 'dlq', jsonb_build_object('healed', healed, 'replayed', items));
  end if;
  return jsonb_build_object('healed', healed, 'replayed', items);
end $function$;

-- Workflows: outreach feedback (Smartlead / Instantly webhooks) ------------

create or replace function public.log_outreach_event(p_token text, p jsonb)
 returns jsonb
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare lid uuid; em text; hashed text; n int; ins boolean; et timestamptz;
begin perform _chk('ingest_token', p_token);
if p->>'gm_lead_id' ~ '^[0-9a-fA-F-]{32,36}$' then select id into lid from leads where id = (p->>'gm_lead_id')::uuid; end if;
if lid is null then for em in select lower(trim(x)) from jsonb_array_elements_text(coalesce(p->'emails','[]'::jsonb)) x loop select id into lid from leads where lower(contact_email) = em limit 1; exit when lid is not null; end loop; end if;
hashed := case when coalesce(p->'emails'->>0,'') = '' then null else encode(extensions.digest(lower(trim(p->'emails'->>0)), 'sha256'), 'hex') end;
et := case when p->>'event_time' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}' then (p->>'event_time')::timestamptz else null end;
insert into outreach_events(event_time, provider, event_type, dedupe_key, campaign_id, lead_id, email_hash, detail) values (et, p->>'provider', p->>'event_type', p->>'dedupe_key', p->>'campaign_id', lid, hashed, p->'detail') on conflict (provider, dedupe_key) do nothing;
get diagnostics n = row_count; ins := n = 1;
if ins and lid is not null then if p->>'event_type' in ('unsubscribe','spam') then update leads set suppressed = true, suppressed_reason = p->>'event_type' where id = lid; elsif p->>'event_type' = 'bounce' then update leads set contact_email_status = 'invalid' where id = lid; end if; end if;
return jsonb_build_object('inserted', ins, 'matched', lid is not null); end $function$;

-- Trigger: tell the enrichment workflow the moment a lead is approved ------
-- Replace YOUR_N8N_WEBHOOK_URL with the production URL of your n8n Webhook node.
-- A failed call is written to audit_log and never blocks the approval.

create or replace function public.notify_approved()
 returns trigger
 language plpgsql
 security definer
 set search_path to 'public', 'extensions'
as $function$
begin
  if new.status = 'approved' and old.status is distinct from 'approved' then
    begin
      perform net.http_post(
        url := 'YOUR_N8N_WEBHOOK_URL',
        headers := '{"Content-Type":"application/json"}'::jsonb,
        body := jsonb_build_object('event', 'lead_approved', 'lead_id', new.id));
    exception when others then
      insert into public.audit_log(actor, action, entity, entity_id, detail)
      values ('db', 'notify_failed', 'lead', new.id, jsonb_build_object('error', sqlerrm));
    end;
  end if;
  return new;
end $function$;

drop trigger if exists leads_notify_approved on public.leads;
create trigger leads_notify_approved
  after update of status on public.leads
  for each row execute function public.notify_approved();
