-- Event Lead Engine: views
-- leads_review is the PUBLIC view: initials and role level only, no company or contact data.
-- leads_review_full is private: it is returned only by review_queue(), which checks a reviewer passcode.

-- Inverse normal CDF (Abramowitz and Stegun 26.2.23), used by sdt_metrics for d-prime.
create or replace function public.probit(p double precision)
 returns double precision
 language sql
 immutable
as $function$
  select case when p <= 0 or p >= 1 then null else (case when p < 0.5 then -1 else 1 end) * (t - (2.515517 + 0.802853*t + 0.010328*t*t) / (1 + 1.432788*t + 0.189269*t*t + 0.001308*t*t*t)) end
  from (select sqrt(-2*ln(least(p, 1-p))) as t) s
$function$;

create or replace view public.leads_review_full as
 select l.id, l.event_id, e.slug as event_slug, e.name as event_name, e.market,
        l.full_name as display_name, l.title, l.company, l.source_url, l.source_kind,
        l.tier, l.score_detail, l.status, l.reject_reason, l.reviewed_at, l.clay_url,
        (l.enrichment is not null) as enriched,
        case when l.status = 'approved' then l.hubspot_payload else null::jsonb end as hubspot_payload,
        l.created_at,
        case when l.status = 'approved' then l.enrichment else null::jsonb end as enrichment,
        l.data_lineage,
        case when l.status = 'approved' then l.contact_email_status else null::text end as contact_email_status,
        case when l.status = 'approved' then l.contact_email_source else null::text end as contact_email_source,
        case when l.status = 'approved' then l.contact_email_at else null::timestamptz end as contact_email_at
   from public.leads l join public.events e on e.id = l.event_id;

create or replace view public.leads_review as
 select l.id, l.event_id, e.slug as event_slug, e.name as event_name, e.market,
        btrim(regexp_replace(l.full_name, '(\S)\S*\s*', E'\\1. ', 'g')) as display_name,
        coalesce(nullif(initcap(btrim(coalesce(l.score_detail->>'function','') || ', ' || coalesce(l.score_detail->>'seniority',''), ', ')), ''), 'Role hidden') as title,
        'Hidden until unlocked'::text as company,
        l.source_url, l.source_kind,
        l.tier, l.score_detail, l.status, l.reject_reason, l.reviewed_at,
        null::text as clay_url,
        (l.enrichment is not null) as enriched,
        null::jsonb as hubspot_payload,
        l.created_at,
        null::jsonb as enrichment,
        l.data_lineage,
        null::text as contact_email_status,
        null::text as contact_email_source,
        null::timestamptz as contact_email_at
   from public.leads l join public.events e on e.id = l.event_id;

create or replace view public.events_public as
 select id, slug, name, market, city, start_date, end_date, date_label, official_url,
        speakers_url, sponsors_url, list_public, found_via, s_buyer, s_icp, s_lead,
        s_timing, s_cost, score, verification, pool_state, notes, source_status
   from public.events;

create or replace view public.review_stats as
 select e.id as event_id, e.slug, e.name, e.market, e.pool_state,
        count(l.id) filter (where l.status = 'pending')  as pending,
        count(l.id) filter (where l.status = 'approved') as approved,
        count(l.id) filter (where l.status = 'rejected') as rejected,
        count(l.id) as total
   from public.events e left join public.leads l on l.event_id = e.id
  group by e.id, e.slug, e.name, e.market, e.pool_state;

create or replace view public.reject_reasons as
 select reject_reason as reason, count(*) as n
   from public.leads
  where status = 'rejected'
  group by reject_reason;

create or replace view public.pipeline_health as
 select (select max(at) from public.audit_log where action = 'ingest_leads') as last_ingest_at,
        (select max(at) from public.audit_log where action = 'review_lead') as last_review_at,
        (select count(*) from public.leads) as leads_total,
        (select count(*) from public.leads where status = 'pending') as pending,
        (select count(*) from public.leads where status = 'approved') as approved,
        (select count(*) from public.leads where status = 'rejected') as rejected,
        (select count(*) from public.leads where status = 'approved' and enrichment is null) as approved_waiting,
        (select count(*) from public.dlq where not resolved) as dlq_open,
        (select count(*) from public.leads l
          where l.status = 'approved'
            and not exists (select 1 from public.audit_log a where a.action = 'source_open' and a.entity_id = l.id)) as approved_without_source_check;

-- Buying-committee coverage per account (private: not granted to the public key).
create or replace view public.account_coverage as
 select min(l.company) as company,
        array_agg(distinct e.slug) as events,
        count(*) as people,
        count(*) filter (where (l.score_detail->>'role') = 'champion') as champions,
        count(*) filter (where (l.score_detail->>'role') = 'economic buyer') as economic_buyers,
        count(*) filter (where (l.score_detail->>'role') = 'technical evaluator') as technical_evaluators,
        ((count(*) filter (where (l.score_detail->>'role') = 'champion') > 0)::integer
         + (count(*) filter (where (l.score_detail->>'role') = 'economic buyer') > 0)::integer
         + (count(*) filter (where (l.score_detail->>'role') = 'technical evaluator') > 0)::integer) as role_coverage,
        count(*) filter (where l.status = 'approved') as approved
   from public.leads l join public.events e on e.id = l.event_id
  where l.tier = any (array['A','B','C'])
  group by lower(l.company);

-- Review quality as signal detection: hit rate, false-alarm rate, d-prime and criterion.
-- Uses public.probit(), defined at the top of this file.
create or replace view public.sdt_metrics as
 with c as (
   select count(*) filter (where status <> 'rejected') as signal_n,
          count(*) filter (where status = 'rejected') as noise_n,
          count(*) filter (where tier = 'A' and status <> 'rejected') as hits,
          count(*) filter (where tier = 'A' and status = 'rejected') as false_alarms,
          count(*) filter (where tier = 'A') as a_reviewed
     from public.leads
    where reviewed_at is not null
 ), z as (
   select c.*,
          public.probit(((c.hits::numeric + 0.5) / (c.signal_n::numeric + 1.0))::double precision) as zh,
          public.probit(((c.false_alarms::numeric + 0.5) / (c.noise_n::numeric + 1.0))::double precision) as zf
     from c
 )
 select signal_n as approved, noise_n as rejected, a_reviewed, hits, false_alarms,
        case when a_reviewed > 0 then round(hits::numeric / a_reviewed::numeric, 3) end as ppv,
        case when signal_n > 0 then round(hits::numeric / signal_n::numeric, 3) end as hit_rate,
        case when noise_n > 0 then round(false_alarms::numeric / noise_n::numeric, 3) end as false_alarm_rate,
        case when signal_n > 0 and noise_n > 0 then round((zh - zf)::numeric, 2) end as d_prime,
        case when signal_n > 0 and noise_n > 0 then round((-0.5::double precision * (zh + zf))::numeric, 2) end as criterion_c
   from z;

-- Outcome rates per scoring exit, once sending starts.
create or replace view public.node_performance as
 with ev as (
   select coalesce(l.score_detail->>'node', 'n/a') as node, l.tier, e.lead_id, e.event_type
     from public.outreach_events e join public.leads l on l.id = e.lead_id
 )
 select node, tier,
        count(distinct lead_id) filter (where event_type = 'sent')     as contacted,
        count(distinct lead_id) filter (where event_type = 'bounce')   as bounced,
        count(distinct lead_id) filter (where event_type = 'spam')     as spam,
        count(distinct lead_id) filter (where event_type = 'reply')    as replied,
        count(distinct lead_id) filter (where event_type = 'positive') as positive,
        round(count(distinct lead_id) filter (where event_type = 'bounce')::numeric   / nullif(count(distinct lead_id) filter (where event_type = 'sent'), 0)::numeric, 4) as bounce_rate,
        round(count(distinct lead_id) filter (where event_type = 'spam')::numeric     / nullif(count(distinct lead_id) filter (where event_type = 'sent'), 0)::numeric, 4) as spam_rate,
        round(count(distinct lead_id) filter (where event_type = 'reply')::numeric    / nullif(count(distinct lead_id) filter (where event_type = 'sent'), 0)::numeric, 4) as reply_rate,
        round(count(distinct lead_id) filter (where event_type = 'positive')::numeric / nullif(count(distinct lead_id) filter (where event_type = 'sent'), 0)::numeric, 4) as positive_rate
   from ev
  group by node, tier;

-- Proposed changes to the scoring tree. A person applies them; nothing changes automatically.
create or replace view public.tuning_flags as
 with p as (select * from public.node_performance),
      tot as (select (sum(p.positive) / nullif(sum(p.contacted), 0::numeric)) as overall from p)
 select p.node, p.tier, p.contacted, p.bounce_rate, p.spam_rate, p.positive_rate,
        case
          when p.contacted::numeric < (select v from public.tuning_rules where k = 'min_contacted') then 'too few sends to judge'
          when p.spam_rate   > (select v from public.tuning_rules where k = 'spam_max')   then 'stop sending to this exit'
          when p.bounce_rate > (select v from public.tuning_rules where k = 'bounce_max') then 'fix the contact data before more sends'
          when tot.overall > 0 and p.positive_rate < tot.overall * (select v from public.tuning_rules where k = 'demote_ratio')  then 'propose: demote this exit one tier'
          when tot.overall > 0 and p.positive_rate > tot.overall * (select v from public.tuning_rules where k = 'promote_ratio') then 'propose: promote this exit one tier'
          else 'hold'
        end as proposal
   from p cross join tot;
