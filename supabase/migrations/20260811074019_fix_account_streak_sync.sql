-- Fix record_daily_visit(): the RETURNS TABLE field visit_date is also a
-- PL/pgSQL variable, so ON CONFLICT (user_id, visit_date) was ambiguous.
-- Referencing the primary-key constraint removes the ambiguity and keeps
-- the function protected by the caller's authenticated RLS context.

create or replace function public.record_daily_visit()
returns table (
  visit_date date,
  first_seen_at timestamptz,
  last_seen_at timestamptz
)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if (select auth.uid()) is null then
    raise exception 'Authentication required';
  end if;

  insert into public.daily_visits as dv (user_id, visit_date)
  values (
    (select auth.uid()),
    (now() at time zone 'Asia/Ho_Chi_Minh')::date
  )
  on conflict on constraint daily_visits_pkey
  do update set last_seen_at = now();

  return query
  select dv.visit_date, dv.first_seen_at, dv.last_seen_at
  from public.daily_visits as dv
  where dv.user_id = (select auth.uid())
  order by dv.visit_date desc
  limit 400;
end;
$$;

revoke all on function public.record_daily_visit() from public, anon;
grant execute on function public.record_daily_visit() to authenticated;
