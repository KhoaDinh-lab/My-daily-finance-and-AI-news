-- Founder Beta: safely collect willingness-to-pay signals and a small,
-- privacy-conscious product funnel. Every signed-in member can only access
-- their own rows through row-level security.

create table public.monetization_leads (
  user_id uuid primary key references auth.users(id) on delete cascade,
  offer text not null default 'founder_monthly'
    check (offer in ('founder_monthly', 'founder_annual', 'sponsor')),
  price_signal integer not null default 49000
    check (price_signal between 0 and 5000000),
  desired_feature text not null default 'personalized_digest'
    check (char_length(desired_feature) between 1 and 80),
  note text not null default ''
    check (char_length(note) <= 1000),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.product_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  event_name text not null
    check (event_name in ('pro_view', 'pro_cta_click', 'pro_interest_submitted', 'sponsor_cta_click')),
  route text not null default 'pro'
    check (char_length(route) between 1 and 40),
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) = 'object' and octet_length(metadata::text) <= 4096),
  occurred_at timestamptz not null default now()
);

create trigger monetization_leads_set_updated_at
before update on public.monetization_leads
for each row execute function public.set_updated_at();

create index product_events_user_time_idx
on public.product_events (user_id, occurred_at desc);

create index product_events_name_time_idx
on public.product_events (event_name, occurred_at desc);

alter table public.monetization_leads enable row level security;
alter table public.product_events enable row level security;

create policy monetization_leads_select_own
on public.monetization_leads for select to authenticated
using ((select auth.uid()) = user_id);

create policy monetization_leads_insert_own
on public.monetization_leads for insert to authenticated
with check ((select auth.uid()) = user_id);

create policy monetization_leads_update_own
on public.monetization_leads for update to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy product_events_select_own
on public.product_events for select to authenticated
using ((select auth.uid()) = user_id);

create policy product_events_insert_own
on public.product_events for insert to authenticated
with check ((select auth.uid()) = user_id);

revoke all on public.monetization_leads, public.product_events from anon;
grant select, insert, update on public.monetization_leads to authenticated;
grant select, insert on public.product_events to authenticated;

revoke all on sequence public.product_events_id_seq from anon;
grant usage, select on sequence public.product_events_id_seq to authenticated;
