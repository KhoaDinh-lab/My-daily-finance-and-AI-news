-- Supabase may inherit broad default privileges for authenticated users.
-- Reset them before granting only the operations the browser actually needs.

revoke all on public.monetization_leads, public.product_events
from public, anon, authenticated;

grant select, insert, update on public.monetization_leads to authenticated;
grant select, insert on public.product_events to authenticated;

revoke all on sequence public.product_events_id_seq
from public, anon, authenticated;

grant usage, select on sequence public.product_events_id_seq to authenticated;
