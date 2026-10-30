# Vartmaan AI backend

One Python backend for the reporter portal, the assignment desk and (later) the digital desk.
It is one codebase that runs as two Render services: the API (`uvicorn app.main:app`) and the
worker (`python -m app.worker`).

## What it does today (Flow 1 and the start of Flow 2)

```
portal / email / WhatsApp
  -> item saved: text in Postgres, files in Supabase Storage (temporary)
  -> slug: the reporter's own slug made folder-safe, else built as DDMMYY_CITY_SUBJECT_TYPE_REPORTER
  -> worker creates FTP <FTP_BASE_DIR>/<slug>/ and uploads files under their original names
  -> size check, then the Storage copy is deleted
  -> desk reviews: edit, pick reporter, approve / reject (reason codes) / reopen
  -> approve creates a story; it enters the Octopus RSS feed once the files are on FTP
  -> Octopus polls GET /feed/<FEED_SECRET>/feed.xml every minute; each poll is logged in feed_log
```

**Rules every channel follows:**

- **Names on FTP**
  - Files keep their original names. Two files with the same name become `IMG_0001.jpg` and `IMG_0001_2.jpg`.
  - WhatsApp photos, videos and voice notes arrive without a name. They are named by time, e.g. `20261017-1042_01.jpg`.
- **Who is the reporter**
  - The sender is matched to the reporter list by phone (WhatsApp) or email (Outlook).
  - If the sender is unknown, or the slug has no usable subject, the item waits as `needs_desk`, and `intake_note` says why.
  - Once the desk picks a reporter, the slug is built and the upload runs.
- **Grouping messages**
  - WhatsApp messages from one sender within `WHATSAPP_MERGE_SECONDS` (default 90) become one item.
  - Email replies in the same thread within 3 days join the same item.
- **Email cleaning**
  - Quoted replies, "Sent from my iPhone", signatures and disclaimers are removed. The raw email is kept in `messages.raw`.
  - Inline images (signature logos) are skipped.
  - OneDrive links are kept as `link_only`.
- **When something fails**
  - One bad email never holds up the inbox. The Outlook cursor always moves on, and the failed email is fetched again by id on the next poll (`kv.mail_retry`).
  - Throttling and Graph outages don't count as attempts. After 3 real failures, or 24 hours, the email is dropped and logged in `kv.mail_dead`.
  - A file over `MAX_UPLOAD_MB` (portal, WhatsApp or Outlook) is not downloaded. It shows as failed: "Ask the reporter to send it by FTP."
- **FTP servers differ:** a server that refuses `SIZE` gets no size check after upload, `MKD` answering 521 or 553 on an existing folder is accepted, `OPTS UTF8 ON` is sent for Hindi file names, and a failed upload removes its `.part` file.
- **The reporter's own slug (build 1)**
  - The slug the reporter typed (subject, else the first line) is kept as `reporter_slug`, with `slug_key`
    (a-z 0-9 only) and `parsed` (date, city token, type, reporter name): `app/services/reporter_slug.py`.
  - The FTP folder name (`slug`) is that slug made ASCII (Hindi transliterated, a Hindi description at the end
    left out, symbols to `_`, max 96, `_2` if taken). Only without one is the slug built the old way.
  - The district comes from the city token (AJM03, alw17, BAL 2, dausa...), else the reporter's district.
    Districts, their aliases and their newsroom FTP folder are the `locations` table (48 rows).
  - `kind`: story, press_release, day_plan or not_a_story (bounces, IMD bulletins, newsletters, unknown email
    senders without a slug). A known reporter's mail is never not_a_story. `/desk/items` shows stories by default.
- **Footage on the newsroom FTP (build 1)**
  - The worker looks for the reporter's own upload in the district folder (`footage_scan`, every
    `FOOTAGE_CHECK_SECONDS`): day folders of the received date and the slug's date, plus the day before and after,
    however the district spells them. Exact or prefix folder names first, then loose files named with the slug;
    a fuzzy match only when date, reporter, type and subject all agree. `app/services/footage.py`.
  - Status: waiting until received + `FOOTAGE_WAIT_HOURS` (4), found (re-listed until then), not_found, manual.
  - **Read-only:** every remote refuses makedirs, put, rename and delete outside `FTP_WRITE_ROOT`.
- **Fetch window:** only mail received at or after `MAIL_FETCH_SINCE` is taken. Empty: the time of the first fetch
  is stored (`kv.mail_fetch_since`), so the first sync never imports the old inbox.
- **Records for training:** every raw message is kept in `messages`. Every desk decision is appended to `decisions`, together with a snapshot of the item as the desk saw it.

The digital desk (Socia) gets its own backend repo and service. The only link between the two is the FTP slug folders.

## Octopus feed

The feed follows the mapping proven against 1st India's Octopus (`phase-1/vartmaan-production-template.xml`):

| Element | Value |
|---|---|
| `<title>` | the slug, with `FLASH ` for breaking stories, `UPDATE ` for corrections, `KILL ` for withdrawals |
| `<description>` | city line, body, reporter line. An UPDATE starts with a `((…))` line naming the slug it replaces, which Octopus leaves out of the read time |
| `<category>` | district name |
| `<guid>` | `vartmaan-{story id}-v{version}`, generated once, never reused (not the slug: reporters resend slugs) |
| `<pubDate>` | the time the story entered the feed (IST) |

- **Approve = publish.** The story waits until the item's files are on FTP, then enters the feed.
- **A published story is never edited.** Octopus ignores a GUID it has already seen. A correction is `POST /desk/items/{id}/publish-update`, which creates a new version. Kill (`POST /desk/items/{id}/kill`) also needs a ninth wire rule in Octopus: Title contains KILL, colour grey, tag KILL.
- **Two locks:** the secret path, and `OCTOPUS_ALLOWED_IPS`. Refused requests get 404, so the path does not advertise itself.
- **Cheap polling:** ETag and Last-Modified headers mean most polls end in a 304.
- **Proof of delivery:** every request, served or refused, goes to `feed_log`. A story's `collected_at` is the first successful poll after it went live.
- **Client IP:** it is read from the end of `X-Forwarded-For`, which a caller cannot forge. After the first deploy, check the IPs in `feed_log` to confirm `TRUSTED_PROXY_HOPS`.

## Shadow mode and queues

- **Blind flag.** A random 10% of items get `blind = true` (`BLIND_RATE`). When the AI panel exists, the desk UI must hide it and must not prefill the AI draft for these items. Their decisions are the clean labels for training.
- **Named queues.** Jobs go to `default` (database work) or `media` (downloads, FTP). `gpu` and `cloud` are reserved for the AI steps. A worker takes the queues it is given:
  ```bash
  python -m app.worker --queues default,media     # Render worker
  python -m app.worker --queues gpu --no-schedule # later, on the GPU box
  ```
  On SIGTERM (every Render deploy), the worker finishes the job in hand, then exits.
- **Embeddings.** `items.embedding` is a 1024-dimension pgvector column with a cosine HNSW index, ready for BGE-M3 duplicate search.

## Not built yet

- Moving rejected folders to `_rejected/`
- The AI pipeline: transcription, tags, policy checks, drafts, and computing embeddings. This waits on the GPU gate and Sarvam API access.
- The digital desk endpoints
- The WhatsApp auto-reply to the reporter
- FTP browsing and attaching files that reporters uploaded straight to FTP

## Run it locally

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"          # Windows; on Mac/Linux: .venv/bin/pip
cp .env.example .env                            # then fill in DATABASE_URL, Supabase, FTP
.venv/Scripts/alembic upgrade head
.venv/Scripts/python -m app.cli create-user --username vansh --name "Vansh" --role super_admin
.venv/Scripts/uvicorn app.main:app --reload     # API on http://localhost:8000, docs at /docs
.venv/Scripts/python -m app.worker              # in a second terminal
```

Tests:

```bash
.venv/Scripts/python -m pytest                  # unit tests: slug, Hindi transliteration, names, email, WhatsApp
.venv/Scripts/python tests/smoke/run_smoke.py   # end to end on a throwaway Postgres + FTP server
```

## Supabase setup

1. **Database.** Copy the connection string from Project settings → Database. Use the **direct** connection or the **session pooler** (port 5432), not the transaction pooler (port 6543).
2. **Storage.** Create a **private** bucket called `intake`.
   - Set the bucket's file size limit to `MAX_UPLOAD_MB`.
   - On the free plan the limit is 50 MB per file. Reporter video needs the Pro plan.
3. **API key.** Copy the **service role** key into `SUPABASE_SERVICE_KEY`. It is used only by the backend; never put it in a frontend.

## Deploy on Render

1. **Create the services.** New → Blueprint → pick this repo. `render.yaml` creates `vartmaan-api` and `vartmaan-worker`.
2. **Fill in the secrets** when Render asks for them. Migrations run before every deploy (`preDeployCommand`).
3. **Add the domain.** Point `api.<domain>` at the API service, then set `CORS_ORIGINS` to `https://report.<domain>,https://desk.<domain>,https://digital.<domain>`.
4. **Create the first users.** Open a Shell on the API service and run:
   ```bash
   python -m app.cli create-user --username vansh --name "Vansh" --role super_admin
   python -m app.cli import-reporters reporters.csv        # name,slug_code,district_code,emails,phone,...
   python -m app.cli backfill-items                         # reporter slug, district, kind for older items
   python -m app.cli reporter-login --phone 919876543210 --username sunita
   ```
5. **Lock down the FTP account.** Samvadia writes only inside `FTP_WRITE_ROOT` (default `/Vartamaan_DND`, enforced in
   code); give the account write rights there only, if the server allows it. `FTP_BASE_DIR` must sit inside it.
   Prefer `ftps` or `sftp` over plain `ftp`.

## How the frontends call it

Every request sends cookies (`credentials: "include"`). Every POST, PATCH, PUT and DELETE also sends the header `X-CSRF: <csrf_token from /auth/login or /me>`.

| Screen | Calls |
|---|---|
| Login (all apps) | `POST /auth/login {username, password}`, `GET /me`, `POST /auth/logout` |
| Reporter: file a report | 1. `POST /reporter/items {headline, body, subject?, story_type?, notes?, tags?, files:[{name,size,content_type}]}` returns `uploads[]`. A tag containing "breaking" sets priority `breaking`<br>2. `PUT` each file to its `upload_url` (body = the file)<br>3. `POST /reporter/items/{id}/submit` |
| Reporter: my reports | `GET /reporter/items`. Each `status` is `submitted`, `reviewing`, `published` or `rejected` |
| Desk inbox | `GET /desk/items?review_state=&source=&q=&before=`, `GET /desk/items/{id}` |
| Desk edit / reporter picker | `PATCH /desk/items/{id} {version, headline?, body?, slug_subject?, story_type?, reporter_id?, category?, priority?, tags?, city_code?, slug?}` (409 if `version` is stale; a hand-typed `slug` sticks until `POST /desk/items/{id}/slug/reset`), `GET /desk/reporters?q=`, `POST /desk/reporters` (admin) |
| Desk pick-lists | `GET /desk/meta` (decision actions, reject and approve reasons with `label_en` / `label_hi`, review states, story types, categories, priorities), `GET /desk/locations` (locations, plus the same codes without labels) |
| Desk fetch history | `GET /desk/fetch-runs` (what each WhatsApp or Outlook pull added) |
| Feed page (admin only) | `GET /desk/stories?item_id=` (state, `collected_at`), `POST /desk/items/{id}/publish-update` and `.../kill`, `GET /desk/feed-log` |
| Desk decisions | `POST /desk/items/{id}/approve {approve_reason?, importance? 1-5, note?}`, `.../reject {reason, note?}` (`other` needs a note), `.../hold`, `.../send-back {note?}`, `.../duplicate {duplicate_of}`, `.../reopen`. Every one also takes `seconds_on_item` or `opened_at`; the body is optional except on reject and duplicate. 409 when the story is in the wrong state |
| Desk retries | `POST /desk/items/{id}/retry-ftp` (admin), `POST /desk/media/{id}/retry`, `POST /desk/refresh` |
| Access page | `GET/POST /admin/users`, `PATCH /admin/users/{id}` |
| Live updates | `new EventSource(API + "/events?app=desk", {withCredentials: true})`, or `app=reporter` |

Sources are `portal`, `outlook` and `whatsapp`. Priorities are `normal`, `high` and `breaking`.
Review states are `pending`, `approved`, `rejected`, `held`, `sent_back` and `duplicate`. Approve and reject come from
`pending` or `held`; hold only from `pending`; send-back and duplicate from `pending` or `held`; reopen takes
`rejected`, `held`, `sent_back` or `duplicate` back to `pending`. An approved story cannot be reopened.
The 13 reject reasons are `duplicate`, `not_newsworthy`, `pr_puff`, `routine_event`, `unverified`, `no_visuals`,
`incomplete`, `wrong_beat`, `too_late`, `legal_risk`, `communal_risk`, `quality` and `other`.
The 7 approve reasons are `must_carry`, `exclusive`, `good_visual`, `audience`, `ready_to_air`, `follow_up` and `filler`.
Reasons and actions live in lookup tables (`reject_reasons`, `approve_reasons`, `decision_actions`), so labels can
change and rows can be switched off without a deploy. Every decision is an append-only `decisions` row (a training
label): a snapshot of the item as the desk saw it before deciding, and on approve the final copy plus
`headline_changed` and `body_edit_distance` against the reporter's original. Full contract for the desk UI:
[../handoff/desk-decisions-v2-contract.md](../handoff/desk-decisions-v2-contract.md) (`c:\vartmaan\code\handoff\desk-decisions-v2-contract.md`).
A story's city is the reporter's city unless the desk sets `city_code`. Changing it rebuilds the slug and renames the FTP folder.
Story types are `AV`, `AVB`, `PKG`, `SF`, `WT`, `PTC`, `BYTE`, `LIVE`, `TEXT` and `BRIEF`. Intake takes the type from
the reporter's slug when it is one of these. If there is none, the slug builder uses `DEFAULT_STORY_TYPE`.

## Owner console API

Used by the Sentry control centre's People page. Server to server only: the browser never calls it.

- Routes: `GET /owner/people`, `GET /owner/people/{user_id}/permissions`, `PATCH /owner/people/{user_id}`, `PUT /owner/people/{user_id}/overrides`, `GET /owner/history`.
- On only when `OWNER_API_KEY` is set to 32+ characters. Unset (or shorter): every `/owner` path is a 404. The same value goes into Sentry's server env as `SAMVADIA_OWNER_KEY`.
- Each call sends `Authorization: Bearer <OWNER_API_KEY>` and `X-Owner-Actor: <sentry username>` (1-64 characters: a-z, 0-9, `.`, `_`, `-`). No cookies, no CSRF.
- Wrong or missing keys are rate limited per IP (20 in 10 minutes). A valid key always passes.
- The last active super admin cannot be switched off, demoted, or have `users.manage`, `page.access` or `desk.view` denied. Such a change gets 409 `last_admin`.
- Errors are `{"error": code, "message": ...}`. Every change is audited with actor `owner:<name>`.
- **Migration:** the overrides table comes from `alembic/versions/0006_user_permission_overrides.py`. Apply it before using the console: `.venv/Scripts/alembic upgrade head` (Windows; `.venv/bin/alembic` elsewhere).

## Where things live

```
app/main.py          FastAPI app and routers        app/worker.py   job loop + mail polling
app/api/             HTTP only                      app/tasks.py    background jobs
app/intake.py        shared intake + finalize       app/jobs.py     Postgres job queue
app/services/        slug, translit, ftp, storage, mail, whatsapp, filenames
app/models.py        tables (migration: alembic/versions/0001_initial.py)
```
