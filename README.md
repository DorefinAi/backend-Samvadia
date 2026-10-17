# Vartmaan AI backend

One Python backend for the reporter portal, the assignment desk and (later) the digital desk.
It is one codebase that runs as two Render services: the API (`uvicorn app.main:app`) and the
worker (`python -m app.worker`).

## What it does today (Flow 1 and the start of Flow 2)

```
portal / email / WhatsApp
  -> item saved: text in Postgres, files in Supabase Storage (temporary)
  -> slug built: DDMMYY_CITY_SUBJECT_TYPE_REPORTER (app/services/slug.py)
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
- **Records for training:** every raw message is kept in `messages`. Every desk decision is appended to `decisions`, together with a snapshot of the item as the desk saw it.

The digital desk (Socia) gets its own backend repo and service. The only link between the two is the FTP slug folders.

## Octopus feed

The feed follows the mapping proven against 1st India's Octopus (`phase-1/vartmaan-production-template.xml`):

| Element | Value |
|---|---|
| `<title>` | the slug, with `FLASH ` for breaking stories, `UPDATE ` for corrections, `KILL ` for withdrawals |
| `<description>` | city line, body, reporter line. An UPDATE starts with a `((…))` line naming the slug it replaces, which Octopus leaves out of the read time |
| `<category>` | district name |
| `<guid>` | `vartmaan-{slug}-v{version}`, generated once, never reused |
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
   python -m app.cli import-reporters reporters.csv        # name,slug_code,city_code,district,phone,email
   python -m app.cli reporter-login --phone 919876543210 --username sunita
   ```
5. **Lock down the FTP account.** It should only be able to write to `FTP_BASE_DIR`. Prefer `ftps` or `sftp` over plain `ftp`.

## How the frontends call it

Every request sends cookies (`credentials: "include"`). Every POST, PATCH, PUT and DELETE also sends the header `X-CSRF: <csrf_token from /auth/login or /me>`.

| Screen | Calls |
|---|---|
| Login (all apps) | `POST /auth/login {username, password}`, `GET /me`, `POST /auth/logout` |
| Reporter: file a report | 1. `POST /reporter/items {headline, body, subject?, story_type?, notes?, tags?, files:[{name,size,content_type}]}` returns `uploads[]`. A tag containing "breaking" sets priority `breaking`<br>2. `PUT` each file to its `upload_url` (body = the file)<br>3. `POST /reporter/items/{id}/submit` |
| Reporter: my reports | `GET /reporter/items`. Each `status` is `submitted`, `reviewing`, `published` or `rejected` |
| Desk inbox | `GET /desk/items?review_state=&source=&q=&before=`, `GET /desk/items/{id}` |
| Desk edit / reporter picker | `PATCH /desk/items/{id} {version, headline?, body?, slug_subject?, story_type?, reporter_id?, category?, priority?, tags?, city_code?, slug?}` (409 if `version` is stale; a hand-typed `slug` sticks until `POST /desk/items/{id}/slug/reset`), `GET /desk/reporters?q=`, `POST /desk/reporters` (admin) |
| Desk pick-lists | `GET /desk/locations` returns locations, categories, priorities, story types and reject reasons |
| Desk fetch history | `GET /desk/fetch-runs` (what each WhatsApp or Outlook pull added) |
| Feed page | `GET /desk/stories?item_id=` (state, `collected_at`), `POST /desk/items/{id}/publish-update` and `.../kill` (admin), `GET /desk/feed-log` (admin) |
| Desk decisions | `POST /desk/items/{id}/approve`, `.../reject {reason, note?}`, `.../reopen` |
| Desk retries | `POST /desk/items/{id}/retry-ftp`, `POST /desk/media/{id}/retry`, `POST /desk/refresh` |
| Access page | `GET/POST /admin/users`, `PATCH /admin/users/{id}` |
| Live updates | `new EventSource(API + "/events?app=desk", {withCredentials: true})`, or `app=reporter` |

Sources are `portal`, `outlook` and `whatsapp`. Priorities are `normal`, `high` and `breaking`.
The reject reasons are `duplicate`, `not_newsworthy`, `poor_visuals`, `unverified` and `other`.
A story's city is the reporter's city unless the desk sets `city_code`. Changing it rebuilds the slug and renames the FTP folder.
Story types are `AV`, `PKG` and `SF`. If the reporter gives none, `DEFAULT_STORY_TYPE` is used.

## Where things live

```
app/main.py          FastAPI app and routers        app/worker.py   job loop + mail polling
app/api/             HTTP only                      app/tasks.py    background jobs
app/intake.py        shared intake + finalize       app/jobs.py     Postgres job queue
app/services/        slug, translit, ftp, storage, mail, whatsapp, filenames
app/models.py        tables (migration: alembic/versions/0001_initial.py)
```
