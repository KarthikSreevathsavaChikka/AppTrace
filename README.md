# AppTrace

A local research workspace for discovering Google Play apps, collecting low-star
reviews, searching reviews by meaning, and downloading original APKs linked to
app records.

## Quick start

Python 3.12 and Node.js 18+ are used in the validated local environment.

```bash
git clone --recurse-submodules https://github.com/KarthikSreevathsavaChikka/AppTrace.git
cd AppTrace
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r vendor/gplay-apk-downloader/requirements.txt
npm ci --prefix discovery-worker --ignore-scripts
.venv/bin/python -m collector.app
```

Open **http://127.0.0.1:5050**. If you cloned without submodules, run
`git submodule update --init --recursive` first. The downloader revision must
match `upstream.json`.

Public listing/review collection does not need Google sign-in. APK downloads use
your own locally authenticated Play account:

```bash
.venv/bin/python vendor/gplay-apk-downloader/gplay-downloader.py auth-account --browser
```

Do not commit or share account tokens. Refresh an expired saved session using:

```bash
.venv/bin/python scripts/refresh_play_session.py
```

## Features

- Keyword/package/Play-link collection with bounded developer and similar-app discovery.
- Original metadata, icons, canonical links, source relationships, and snapshots.
- Targeted 1-, 2-, and 3-star reviews with per-rating/locale coverage information.
- Background collection explorer distinguishing new, reused, and refreshed apps.
- Local EmbeddingGemma 2 indexing and semantic review search.
- Beta/testing metadata hints plus manually recorded verification evidence.
- Sequential APK queue with stop/cancel/resume, configurable pacing, bounded
  retries, Retry-After handling, and verified-version reuse.
- Manifest package/version checks, original base/split files, SHA-256 inventory,
  and global APK statistics.
- Excel, CSV, and JSON exports.

## Optional review embeddings

```bash
.venv/bin/python -m pip install -r requirements-embeddings.txt
.venv/bin/python scripts/prepare_embeddings.py
```

Restart the server, select **Review intelligence**, then **Index saved reviews**.
The pinned model revision is in `embedding_model.json`; weights are cached in
`.models/`. Review vectors are stored in SQLite. Local inference does not send
review text to a model API. Semantic similarity is relevance ranking, not a
verified allegation, sentiment score, or risk probability. Complaint classification,
calibrated concern scoring, and APK behavior analysis are not implemented.

## Storage and sharing

```text
data/
  collector.sqlite
  icons/
  apks/<package>/<version>/<architecture>/<attempt>/
    base.apk
    split-*.apk
```

SQLite links each file to the app, version, requested architecture, path, size,
and hash. APK binaries are stored locally, not in SQLite. Share a consistent
SQLite backup together with its `apks/` folder; preserve relative paths. The Excel
export can sit beside the database and folder as a file/status index. It does not
contain APK binaries. Never include Google auth files or account credentials.

The repository excludes collected data, APKs, exports, model weights, virtual
environments, node_modules, credentials, and local tool state. New clones start
with an empty workspace.

## Collection limits and interpretation

Up to 20 seeds, 50 search results per keyword, 1,000 apps per run, five discovery
levels, and 10,000 reviews per star rating are configurable ceilings, not promises
of complete coverage. The presets only begin a bounded crawl; there is no recurring
scheduler. Related links may include non-games. Missing low-star reviews or a
`.beta` package-name hint do not establish safety or the actual APK release track.

Downloads remain sequential. Pacing cannot guarantee acceptance by Google Play;
account, region, compatibility, and service restrictions can prevent acquisition.
Failed/paused attempts remain visible. Partial files restart from the beginning.
Manifest/ZIP validation does not verify signing certificates or guarantee every
Play Asset Delivery pack/variant is present.

## Development and verification

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check collector/static/app.js
node --check discovery-worker/expand.mjs
```

`requirements.lock.txt` records the full validated environment, including optional
embedding dependencies. See `THIRD_PARTY_NOTICES.md` for upstream attribution.
Run the local Flask server as a single process on loopback. It has no multi-user
authentication or production job service; network hosting needs additional work.

## Detailed local workflows

The reference below documents individual components and development milestones.
Where an older milestone describes a lower limit or missing feature, the feature
summary and current implementation above take precedence.

# Play Store collection: downloader validation

First milestone: validate the existing downloader before building the crawler.
The upstream checkout and revision are recorded in `upstream.json`; it retains
its upstream GPL-3.0 license. No upstream source has been modified.

## Environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r vendor/gplay-apk-downloader/requirements.txt
```

Use `requirements.lock.txt` to reproduce the dependency versions installed here.
Raw download validation does not require Java, APKEditor, or apksigner.

## Authentication

Run this locally in an interactive terminal and complete Google sign-in:

```bash
.venv/bin/python vendor/gplay-apk-downloader/gplay-downloader.py auth-account --browser
```

The upstream tool stores authentication in `~/.gplay-auth.json` and an ARMv7
companion file. Keep these local; do not paste account tokens into chat or commit
them. Alternatively, use the upstream `auth --dispenser URL` command with a
dispenser you control. No default dispenser is supplied.

## Validate downloads

```bash
.venv/bin/python scripts/validate_download.py com.example.app com.example.other --arch arm64
```

Replace the example IDs with actual target packages. Each run gets a separate
directory under `artifacts/`, retaining original base and split APK files without
merging, signing, installing, or executing them. Its `report.json` records the
upstream revision, package ID, Play URL, time, exit status, sizes, hashes, and ZIP
integrity/manifest checks. Failed or partial outputs remain available for review.
These checks do not verify signatures or guarantee all delivery artifacts were
downloaded. Asset-pack coverage needs separate validation.

Live acceptance: authenticate, download a small free app and a split-based target
app, inspect the reports, and verify package/version and signatures using Android
SDK tools before accepting the downloader integration. Authentication or region
restrictions must be recorded as failures, not interpreted as successful coverage.

## Collection workspace UI

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m collector.app
```

Open **http://127.0.0.1:5050**. Choose **New collection**, enter keywords,
package IDs, or Play Store links (one per line), and configure country, language,
review count, and crawl limits. The suggested seeds use the research keywords
from this project. Public listing/review collection does not use Google login.

The interface includes a searchable app library, category filters, app detail
and review inspection, copyable Play links, background progress, activity logs,
a stop control, and CSV/JSON exports. The detail panel can start another
collection to explore an app's connections. No sample records are fabricated.

Metadata, snapshots, reviews, discovery edges, and job history persist in
`data/collector.sqlite`; icons are downloaded into `data/icons/`. JSON exports
include snapshots, relationships, reviews, jobs, and event history; CSV exports
contain the current app metadata summary. Original seed URLs remain in discovery
relationships. Repeated reviews are deduplicated by package, region, language,
and review ID. Current metadata reflects the latest collected locale, while
snapshots retain prior collections. Each request retries up to three times;
requests have timeouts and collections are paced. Only one collection runs at a
time. Stopping takes effect after the current in-flight work; interrupted jobs
are marked on server restart and can be rerun through a new collection.

Developer and recommendation discovery inspect links available on Play pages.
Coverage is best effort: recommendation links are candidates, not a guaranteed
complete list or a determination of gambling behavior. Reviews are bounded,
newest-first, and library pagination is used up to the configured count; this
is not a claim to collect every review. Related apps can cross category boundaries.
Collection failures and missing sections are surfaced in the activity log.
APK download integration and APK analysis are not yet exposed in the UI.

Run the local server as a single process on loopback; it has no multi-user login
or production job runner. Internet access is required for live collection.
Typography uses optional Google Fonts with system fallbacks.

Validation:

```bash
.venv/bin/python -m unittest discover -s tests -v
node --check collector/static/app.js
```

App cards distinguish Play Store-reported review totals from reviews collected
locally. App details show rating totals separately: ratings and written reviews
are different measures. Unavailable totals are displayed as unavailable, and
counts reflect the listing locale and time of collection, not guaranteed global
coverage. Existing metadata records also expose these totals without re-scraping.
CSV/JSON app exports include `total_reviews`, `total_ratings`, and `saved_reviews`.
Use **All developers** to filter by developer identity, or **Group by developer**
to organize the library. App details allow filtering saved reviews to 1–2 or 1–3
stars. This is a rating filter, not sentiment classification or embedding analysis.

Discovery now reads the named **More by …** and **Similar games/apps** sections
separately (English page headings, selected country). Other links on the listing
are excluded. Both relationships are retained if an app appears in both sections.
Section availability varies; missing sections generate coverage warnings.
The current implementation collects apps visible in these sections, not all pages
behind their “see more” links.

Before collecting each queued package, the worker checks the database. Existing
apps from the same country/language reuse their saved metadata/reviews by default,
while new discovery relationships are still saved and exploration continues up
to the selected depth. Choose **Refresh already collected apps** for a fresh
snapshot and reviews. Another locale also triggers collection. New packages are
collected once per run, preventing cycles; configured app/depth limits still apply.
Job activity shows new collections and reused records separately.

### Expanded discovery and connection lists

Install the pinned pagination worker dependencies once:

```bash
npm ci --prefix discovery-worker --ignore-scripts
```

Discovery now follows the exact **See more** section links and uses continuation
pagination, bounded by the collection's maximum-app setting (up to 200 candidates
per section). This supersedes the visible-section-only limitation above. The
worker uses the locked Node scraper dependency; the top-level parser version is
pinned for Node 18 compatibility. If a page or continuation format changes, a
warning is logged and visible section candidates are retained; complete coverage
is not promised.

Open an app's details to see **More apps by this developer** and **Similar games /
apps**. Each list shows recorded relationships across collections, with Collected
or Discovered status. Collected apps open in the tool; uncollected apps link to
Play Store. Discovered records beyond the job's collection limit remain in the
list, without being presented as fully scraped. Run Explore connections again to
expand older saved app relationships using See more.

### Live collection explorer

New collections record a durable per-app queue in `job_apps`. The Collection
Explorer shows queued/processing/completed/failed states and separates newly
collected, reused, and refreshed apps. Each row retains the original seed,
starting app, immediate discovery source, relationship, and depth. When several
paths reach the same app, the row shows the first queued path; all relationships
remain recorded in `edges`. Choose a prior run or filter by acquisition/status.
Collections created before this feature retain their logs but do not have
backfilled per-app tracking. Restart the server and start a new collection to
use it. JSON exports include the per-app history.

## Review embeddings and semantic search

Review intelligence runs locally with **google/embeddinggemma-2** (text-only,
768 dimensions). `embedding_model.json` pins its revision. Install its CPU
requirements, prepare the cache, then restart the app:

```bash
.venv/bin/python -m pip install -r requirements-embeddings.txt
.venv/bin/python scripts/prepare_embeddings.py
.venv/bin/python -m collector.app
```

Select **Review intelligence → Index saved reviews**. Background indexing
prioritizes 1–2-star reviews, shows progress and coverage, and can be stopped.
Indexing is incremental: repeated text reuses a vector; changed review text or a
new model revision requires indexing again. A server restart marks incomplete
jobs interrupted; click Index saved reviews to resume using cached vectors.
Empty review text is excluded from eligible coverage. SQLite contains the vectors
as float32 BLOBs, their text hashes, model/prompt identity, review associations,
and indexing history. The model weights are separate in the ignored `.models/`
cache; recipients of a shared database need the matching model to embed queries.
Inference uses the local cache only; review text is not sent to a model API.

Search supports app and star filters, with 1–2 stars selected by default. Search
results preserve review text, rating, app, locale, version, and developer reply.
App detail panels also have a **Search reviews** shortcut. Similarity is a ranking
measure, not sentiment, complaint verification, confidence, or a risk score.
Search can return weak matches when the corpus lacks the requested issue.
Complaint classification/grouping and calibrated risk scoring remain future work.
English/Hindi/Hinglish retrieval needs a labelled evaluation before claims of
reliable concern detection; the integration smoke check is not an accuracy study.

New collections can request **Extra reviews per low-star rating** in addition to
newest reviews. For example, 50 requests up to 50 one-star and 50 two-star reviews;
review IDs deduplicate overlap. Enable Refresh already collected apps when adding
these reviews to previously saved apps, because reuse skips review collection.
Library cards distinguish listing totals, saved review counts, and saved low-star
counts. Rating filters do not detect negative higher-rated reviews.

Offline real-model integration check (temporary database, 12 saved reviews):

```bash
.venv/bin/python scripts/check_embeddings.py
```

Model documentation: https://huggingface.co/google/embeddinggemma-2

## Local APK storage and batch downloads

The current APK implementation stores original files locally, **not as SQLite
BLOBs**. Restart the app after installing updated requirements. In App library,
select app-card checkboxes and choose **Download selected**, or use **Download
all apps** for the entire saved library (not just filtered results). Choose ARM64
or ARMv7. Only one APK batch runs at a time, separately from review collection.
The **APK downloads** screen shows queued, downloading, downloaded, or failed
status per app. App detail panels show download history and links to saved files.

The existing local Google Play authentication is reused; ARMv7 requires its
architecture-specific auth file. A saved login can expire. If authentication is
missing, run the upstream auth-account command locally; credentials are not
entered in the web UI. Region/device/account limitations can prevent downloads.
Paid apps are rejected by the upstream downloader. No APK is merged, re-signed,
installed, or executed. Repeating a download creates a new queue entry; the paced queue below checks
for verified local versions before transferring files and applies bounded retries.

Files are placed relative to the database directory:

```text
data/
  collector.sqlite
  apks/
    com.example.game/
      12345/
        arm64/
          <download-id>/
            base.apk
            split-001.apk
```

SQLite `apk_downloads.package` links to `apps.package`. Each `apk_files` row links
to a download and records the relative path, original split identity, byte size,
and SHA-256. Before registration, the worker checks ZIP integrity and verifies
manifest package/version consistency, exactly one base APK, and unique split
identities. This verifies association, not signing certificates or completeness
of all Play Asset Delivery packs/device variants. Architecture records the
requested profile, not an independently verified device ABI.

A download is marked successful only after validation and file/database storage.
Partial staging files are temporary; a crash between final file movement and DB
commit may leave an unregistered directory. On restart, unfinished work is paused and can be resumed in its existing batch. The single-process local server owns
this background queue; no multi-process job runner is configured.

For sharing, share a consistent database snapshot **together with its `apks/`
folder**, preserving relative paths. Database/JSON exports contain references,
not APK binaries. SQLite WAL writes must be checkpointed safely or copied using
SQLite backup before sharing; don't copy only an actively written main file.

Live one-app integration check (temporary database/files):

```bash
.venv/bin/python scripts/check_apk_download.py PACKAGE_ID
```

### Expired Play sessions

A token file being present does not mean it is still accepted. HTTP 401 indicates
Google Play rejected the saved session. Refresh from the saved long-lived AAS
token without placing credentials in command arguments:

```bash
.venv/bin/python scripts/refresh_play_session.py
```

This updates ARM64 and ARMv7 session files locally with mode 0600. If no AAS token
is available or refresh fails, run the upstream browser sign-in command instead.
Restart AppTrace to load code changes, then test one app before starting a new
batch. Old failure records remain as history. The downloader now reports known
401, DNS/network, dependency, and unavailable-app failures without exposing raw
logs. Shared session/network/dependency failures pause the batch and preserve its
remaining queue, preventing repeated calls with the same broken environment. App-specific failures allow the batch to proceed.

### Paced, stoppable, resumable APK queue

New batches expose pacing settings in App library: default 10 seconds between
apps, groups of 10 with an additional 60-second break, and at most two retries
for temporary HTTP/network failures. These are conservative workload controls,
not Google-approved limits or a guarantee against restrictions. Settings apply
to new batches; existing batches retain their saved/default settings.

In **APK downloads**, select a batch to inspect its progress:

- **Cancel app** stops one queued or active app; other apps continue.
- **Stop batch** terminates the active downloader and cancels remaining queued
  apps. Already registered files remain untouched.
- **Resume unfinished apps** restarts paused/failed work and apps cancelled by
  Stop batch. Individually cancelled apps stay cancelled. Downloaded/skipped apps
  are not requeued. Resume after a restart preserves completed work.

Partial active downloads restart from the beginning; byte-range download resume
is not implemented. The child process group is terminated for active cancellation;
validation may finish its current check before acknowledging cancellation.

Before downloading, a private Play details request checks the available version
for the selected architecture without a public-page fallback. An existing local
package/version/requested-architecture set is reused only after file existence,
size, and SHA-256 checks. A skipped entry references those original file records.
The cache key does not distinguish every possible device-profile variant.

The isolated transport worker reads HTTP statuses and Retry-After headers without
storing raw authentication logs. HTTP 429 pauses the batch, and Resume is blocked
until the server's waiting period expires (60 seconds when no valid Retry-After
is given). HTTP 401 pauses for local session refresh; 403 pauses for investigation.
Transient network/5xx failures use bounded exponential backoff before pausing.
Other app-specific failures are recorded and the queue continues. There is no
account rotation or automatic bypass of restrictions.

When upgrading an actively running old server, avoid a second batch. Stop the old
server once, restart with the new code, then select the existing batch and Resume
unfinished apps. Startup migrations pause unfinished records instead of marking
every queued app failed. Successful files and download records are preserved.

## Low-star-focused automatic discovery

New collections use `review_mode=low_only`: request 1-, 2-, and 3-star reviews
separately, up to the configured **Reviews per star rating** limit for each.
For example, 50 means at most 150 returned review records before deduplication.
No 4- or 5-star review fallback is fetched. Older high-star records remain as
history; indexing and semantic search now consider only 1–3-star text. New
collections fill missing/failed low-star coverage even when app metadata is
reused. Refresh existing apps to deliberately update already checked strata.

App details show each star's reported rating count, stored written-review count,
and locale-specific collection status. `review_checks` distinguishes successful
returns, empty returns, failed fetches, and unattempted strata. Empty returns
mean no written reviews were returned by that request, not proof that no such
rating/review exists worldwide. Missing text means insufficient review evidence;
no concern/risk score is inferred from missing data. Rating histogram values can
still provide rating-level evidence without written comments.

The **automatic game-crawl preset** supplies game-search seeds, enables both
same-developer and similar-game discovery, and selects depth 2. Once started,
the background worker follows those connections without clicking each app.
Depths up to 5 and the existing app limit bound exploration. This is one bounded
crawl, not a recurring schedule or unlimited crawl. Suggested/developer links
are discovery candidates and can include apps outside gaming; categories remain
visible for review. An app's absence of low-star reviews does not stop discovery
of its related/developer apps.

## Shareable Excel inventory

**App library → Export Excel** downloads `AppTrace-collection.xlsx` with:

- **Read me:** sharing instructions and status definitions.
- **Apps:** app/developer/package details, low-star review counts, latest APK
  status/error, and registered-file totals across successful downloads.
- **APK downloads:** every attempt, including failed/cancelled/pending work and
  references to reused versions.
- **APK files:** each registered base/split APK's filename, relative path,
  package/version/architecture, size, and SHA-256.

The workbook is a consistent point-in-time database read; downloads continuing
later are not included until the next export. It contains file references, not
APK binaries. Place the workbook next to `collector.sqlite` and the `apks/`
folder when sharing so relative file links can work. Recipients can inspect the
workbook in Excel or LibreOffice without AppTrace. The Apps sheet distinguishes
a failed latest attempt from earlier registered files. Text fields are written
as text to prevent spreadsheet formula execution.

### Expanded presets and collection limits

The game-crawl preset now contains 20 editable search seeds. Each non-empty line
in Seeds is one starting keyword, package ID, or Play Store URL; up to 20 are
accepted per collection. With results-per-keyword 10, twenty keywords produce at
most 200 starting search matches before deduplication. Developer/similar discovery
then continues within the overall app/depth limits.

New collection ceilings are 1,000 apps and 10,000 reviews **per star rating**
(1, 2, and 3 only); defaults remain 30 apps and 50 reviews per star rating.
The ceilings do not guarantee availability and are not a recommendation to fetch
the maximum on every run. Low-star review continuations are requested in pages
of up to 200, deduplicated, with a one-second pause between pages and stop checks.
The final page is trimmed to the requested count. Repeated/stalled continuation
responses or a stop request are recorded as partial coverage. Developer/similar
See more expansion remains bounded to 200 candidates per section; the 1,000-app
setting is a total crawl limit across sections and starting apps.

## Beta and testing evidence

**Beta & testing** evaluates all saved app metadata on read, so old and new
collections receive the same treatment without re-scraping. Explicit tokens in
titles, version names, package segments, or developer testing statements produce
**possible** labels only. Missing reviews, small version numbers, and low ratings
are not beta/testing indicators. The public scraper has no authoritative access
to every app's release tracks or tester enrollment.

The section filters possible testing, beta-program availability, manually verified
testing, and unknown status. App details allow manual evidence notes with a public
HTTPS URL and observed version. Confirmed labels are explicitly manual assessments,
not machine-verified release tracks. Beta-program availability does not establish
the downloaded APK's track. When saved listing versions change, manual evidence
for an older version is downgraded to historical/current-version-unverified.
Absence of a hint means **not established**, not production verified.

Testing status is independent of APK download status. Evidence fields appear in
Excel and JSON exports. Resetting to automatic evidence removes a manual override
but can leave metadata-based hints visible. No tester program is automatically
joined. Closed/internal testing can be invisible in public search; beta feedback
may be private, so missing public reviews are not proof of either testing or safety.

References:
https://support.google.com/googleplay/android-developer/answer/9845334
https://support.google.com/googleplay/answer/7003180

### Shorter APK pauses and transfer overhead

**APK downloads → Use shorter pauses** changes the selected batch to a two-second
inter-app gap, groups of 20, and a 15-second extra group break. The updated worker
reloads these settings after each app, so changing them does not interrupt an
active transfer. Older server processes must be restarted to load this behavior.
New-batch defaults remain configurable in the library.

The transport worker reuses a requests Session for API/file requests within one
app attempt, buffers streaming reads at 256 KiB instead of the upstream 8 KiB,
and drops repetitive progress prints while retaining bounded failure context.
Mid-stream request errors now receive transient-error handling and bounded retry.
Apps remain sequential and 401/403/429 pause behavior is preserved. These changes
reduce idle/processing overhead; they cannot guarantee faster CDN/ISP throughput.
An interrupted partial file still restarts from the beginning.

### Global APK statistics

The APK downloads page now shows all-batch statistics separately from the selected
batch: unique apps with registered downloaded APKs, library coverage, base/split
file counts, registered file sizes, free disk space, unique package/version/
architecture sets, successful attempts, reused attempts, failed attempts, and
queued/downloading/paused/cancelled work. Skipped references do not count the
same file records twice. Multiple separately downloaded copies do occupy storage
and count separately. Historical failure attempts are not a count of apps that
currently lack APKs. Registered totals are database inventory, not a recurring
filesystem/hash verification scan; manually removed files can remain referenced.
