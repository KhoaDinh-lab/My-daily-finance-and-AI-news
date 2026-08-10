create table public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default 'Thành viên' check (char_length(display_name) between 1 and 80),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table public.daily_visits (
  user_id uuid not null references auth.users(id) on delete cascade,
  visit_date date not null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  primary key (user_id, visit_date)
);

create table public.article_reads (
  user_id uuid not null references auth.users(id) on delete cascade,
  article_key text not null check (char_length(article_key) between 1 and 2048),
  section text not null default 'news' check (char_length(section) between 1 and 40),
  title text not null default '' check (char_length(title) <= 500),
  first_read_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, article_key)
);

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

create trigger article_reads_set_updated_at
before update on public.article_reads
for each row execute function public.set_updated_at();

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, display_name)
  values (
    new.id,
    coalesce(
      nullif(btrim(new.raw_user_meta_data ->> 'display_name'), ''),
      nullif(split_part(coalesce(new.email, ''), '@', 1), ''),
      'Thành viên'
    )
  )
  on conflict (id) do nothing;
  return new;
end;
$$;

create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_user();

insert into public.profiles (id, display_name)
select
  u.id,
  coalesce(
    nullif(btrim(u.raw_user_meta_data ->> 'display_name'), ''),
    nullif(split_part(coalesce(u.email, ''), '@', 1), ''),
    'Thành viên'
  )
from auth.users u
on conflict (id) do nothing;

alter table public.profiles enable row level security;
alter table public.daily_visits enable row level security;
alter table public.article_reads enable row level security;

create policy profiles_select_own on public.profiles for select to authenticated
using ((select auth.uid()) = id);
create policy profiles_insert_own on public.profiles for insert to authenticated
with check ((select auth.uid()) = id);
create policy profiles_update_own on public.profiles for update to authenticated
using ((select auth.uid()) = id) with check ((select auth.uid()) = id);

create policy daily_visits_select_own on public.daily_visits for select to authenticated
using ((select auth.uid()) = user_id);
create policy daily_visits_insert_own on public.daily_visits for insert to authenticated
with check ((select auth.uid()) = user_id);
create policy daily_visits_update_own on public.daily_visits for update to authenticated
using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

create policy article_reads_select_own on public.article_reads for select to authenticated
using ((select auth.uid()) = user_id);
create policy article_reads_insert_own on public.article_reads for insert to authenticated
with check ((select auth.uid()) = user_id);
create policy article_reads_update_own on public.article_reads for update to authenticated
using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);

create or replace function public.record_daily_visit()
returns table (visit_date date, first_seen_at timestamptz, last_seen_at timestamptz)
language plpgsql
security invoker
set search_path = ''
as $$
begin
  insert into public.daily_visits (user_id, visit_date)
  values ((select auth.uid()), (now() at time zone 'Asia/Ho_Chi_Minh')::date)
  on conflict (user_id, visit_date)
  do update set last_seen_at = now();

  return query
  select dv.visit_date, dv.first_seen_at, dv.last_seen_at
  from public.daily_visits dv
  where dv.user_id = (select auth.uid())
  order by dv.visit_date desc
  limit 400;
end;
$$;

revoke all on public.profiles, public.daily_visits, public.article_reads from anon;
grant select, insert, update on public.profiles to authenticated;
grant select, insert, update on public.daily_visits to authenticated;
grant select, insert, update on public.article_reads to authenticated;

revoke all on function public.set_updated_at() from public, anon, authenticated;
revoke all on function public.handle_new_user() from public, anon, authenticated;
revoke all on function public.record_daily_visit() from public, anon;
grant execute on function public.record_daily_visit() to authenticated;
