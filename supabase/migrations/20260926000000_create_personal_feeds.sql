-- Danh mục tin cá nhân hoá: mỗi thành viên tự tạo danh mục riêng bằng cách
-- trò chuyện với AI. AI chỉ chạy một lần để sinh ra bộ quy tắc lọc (spec);
-- việc lọc và xếp hạng tin hằng ngày chạy miễn phí trên trình duyệt.
--
-- Mọi dữ liệu được bảo vệ theo UID bằng row-level security.

create table public.user_feeds (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  name text not null
    check (char_length(btrim(name)) between 1 and 80),
  spec jsonb not null
    check (
      jsonb_typeof(spec) = 'object'
      and octet_length(spec::text) <= 8192
      and jsonb_typeof(spec -> 'include_keywords') = 'array'
      and jsonb_array_length(spec -> 'include_keywords') between 1 and 20
    ),
  source_prompt text not null default ''
    check (char_length(source_prompt) <= 2000),
  is_active boolean not null default true,
  sort_order smallint not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index user_feeds_owner_idx on public.user_feeds (user_id, sort_order, created_at);

create trigger user_feeds_set_updated_at
before update on public.user_feeds
for each row execute function public.set_updated_at();

-- Hành vi đọc theo từng danh mục. Đây là nguyên liệu cho P2 (tự học sở thích)
-- và cũng là nơi đếm số lần gọi AI để chặn đốt quota Groq.
create table public.feed_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  feed_id uuid references public.user_feeds(id) on delete cascade,
  article_key text not null default ''
    check (char_length(article_key) <= 2048),
  event_name text not null
    check (event_name in (
      'feed_compose',
      'feed_impression',
      'feed_open',
      'feed_dismiss',
      'feed_save'
    )),
  rank_position smallint not null default 0
    check (rank_position between 0 and 200),
  score numeric(6, 4) not null default 0,
  occurred_at timestamptz not null default now()
);

create index feed_events_user_time_idx on public.feed_events (user_id, occurred_at desc);
create index feed_events_feed_idx on public.feed_events (feed_id, occurred_at desc);
create index feed_events_name_time_idx on public.feed_events (event_name, occurred_at desc);

-- Gói miễn phí: tối đa 3 danh mục. Chặn ở tầng cơ sở dữ liệu vì không bao giờ
-- được tin vào giới hạn phía trình duyệt.
create or replace function public.enforce_feed_quota()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  feed_count integer;
begin
  select count(*) into feed_count
  from public.user_feeds
  where user_id = new.user_id;

  if feed_count >= 3 then
    raise exception 'FEED_QUOTA_EXCEEDED'
      using hint = 'Gói miễn phí cho phép tối đa 3 danh mục.';
  end if;

  return new;
end;
$$;

create trigger user_feeds_quota
before insert on public.user_feeds
for each row execute function public.enforce_feed_quota();

alter table public.user_feeds enable row level security;
alter table public.feed_events enable row level security;

create policy user_feeds_select_own
on public.user_feeds for select to authenticated
using ((select auth.uid()) = user_id);

create policy user_feeds_insert_own
on public.user_feeds for insert to authenticated
with check ((select auth.uid()) = user_id);

create policy user_feeds_update_own
on public.user_feeds for update to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

create policy user_feeds_delete_own
on public.user_feeds for delete to authenticated
using ((select auth.uid()) = user_id);

create policy feed_events_select_own
on public.feed_events for select to authenticated
using ((select auth.uid()) = user_id);

create policy feed_events_insert_own
on public.feed_events for insert to authenticated
with check ((select auth.uid()) = user_id);

revoke all on public.user_feeds, public.feed_events from anon;
grant select, insert, update, delete on public.user_feeds to authenticated;
grant select, insert on public.feed_events to authenticated;

revoke all on sequence public.feed_events_id_seq from anon;
grant usage, select on sequence public.feed_events_id_seq to authenticated;
