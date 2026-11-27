# Deployment reference notes

These notes were retained from the original deployment and sanitized for this
repository. Paths and Discord identifiers are examples. Historical test counts
and deployment statements describe that environment, not validation on a new host.
Use the root README for the publication snapshot and development quickstart.

# Jarvis Discord Bot

A J.A.R.V.I.S.-inspired Discord front end for the Codex CLI already installed on
your laptop. It reuses the CLI's saved ChatGPT login, so no separate OpenAI API
key or API billing is required. Messages beginning with `Jarvis`, directly
mentioning the bot, or replying to one of its messages with Discord's **@ on
reply** option enabled are sent to Codex, and
conversation sessions survive bot restarts.

## Behavior

- In DMs, every human message invokes Jarvis; no name, mention, or reply is
  required. Owners retain owner access; everyone else uses the restricted public
  interface and its usage limits. Channel allowlists apply to server channels.
- `Jarvis, give me a status report` asks the assistant a question.
- `@Jarvis give me a status report` works as well. Replies invoke Jarvis only
  when Discord's **@ on reply** option is enabled.
- `Jarvis /memory` shows how many messages are stored for you in that channel.
- `Jarvis /forget` erases your direct conversation memory for that channel.
- `Jarvis /forget-chat` erases stored ambient text for the current channel.
- Attach a PNG, JPEG, GIF, or WebP to `Jarvis /pfp` to change the bot's global
  Discord avatar (maximum 10 MiB).
- `Jarvis /pfp reset` restores the bot's default avatar.
- `Jarvis /help` shows the available commands.
- Everyone can create a personal timed reminder in DMs or enabled public
  channels: `Jarvis remind me to check this link in 3.5 hours` or
  `Jarvis remind me in 30 minutes to stretch`. Seconds, minutes, hours, days,
  weeks and compound delays are supported, up to 365 days. The reminder pings
  only its requester in the same conversation; it never executes its contents.
  Use `Jarvis /reminders` to list your reminders in that conversation, and
  `Jarvis cancel reminder 12` to cancel your own pending reminder. There is a
  maximum of ten active reminders per user. This deterministic service uses no
  model quota and does not delegate to R. Pending jobs persist across restarts;
  interrupted or ambiguous deliveries are retained as `uncertain` for review
  rather than automatically posting duplicates.
- `Jarvis containers` returns a detailed owner-only report of OMEN's containers
  without invoking Codex. `Jarvis containers details`, `/containers`, and
  `Jarvis docker status` also work. Public users and other bots cannot request it.
- Direct conversation memory is isolated by server, channel, and user.
- Durable name-equivalence groups in `data/server-identities.json` are supplied
  as factual reference to owner, public, and bot conversations.
- Jarvis backfills text history from every accessible server channel and
  continuously stores new text with server, channel, user, and message metadata.
- Set `CHAT_HISTORY_BACKFILL_LIMIT=0` to archive all history Discord permits the
  bot to read. Bot and webhook messages are archived as context only, apart from
  the explicitly addressed R image-generation exception described below.
- Logged channel messages are retained in SQLite. Discord deletion events mark
  records as deleted rather than removing them; deleted records are excluded from
  future Codex context.
- Ordinary messages are stored as channel context but invoke no action. Recent
  context is sent to Codex only when an authorized user invokes Jarvis by name,
  mention, or reply.
- New Discord voice notes in configured channels are transcribed locally and the
  transcript is posted automatically. A transcript invokes Codex only when its
  original Discord author is authorized and the speech invokes Jarvis.
- Optional live VC notes keep Jarvis visibly connected to one configured voice
  channel, segment each member's PCM stream, transcribe it locally, and retain
  timestamped speaker notes for a configurable period. Raw live audio is never
  retained.
- Jarvis leaves after the configured VC has had no human members for five
  minutes. Other bots do not keep him there. This disables listening until an
  owner explicitly enables it again; the reconnect loop will not rejoin.
- Saying `Jarvis` in the monitored VC routes the completed spoken request to the
  paired text channel. Saying only `Jarvis` opens a short follow-up window for
  that same speaker. Spoken requests are read-only by default, including owner
  requests; consequential actions must be confirmed in text.
- `Jarvis /vc-summary 30` summarises the preceding 30 minutes for members who can
  view the source voice channel. Owners can use `Jarvis ntoggle` as the simple
  persistent on/off control, or use `Jarvis /vc-notes status`,
  `pause`, `resume`, `delete 30`, and `delete all`.
- Stored transcripts can also be queried by local time, day, or topic: `Jarvis
  notes yesterday`, `Jarvis notes from 2pm to 4pm today`, and `Jarvis notes
  yesterday about deployment`. Queries use `LIVE_VOICE_TIMEZONE` and require
  permission to view the source VC. Transcripts persist until an owner deletes
  them; raw audio is discarded after local transcription.
- Discord user IDs listed in `AUTHORIZED_USER_IDS` are owners and retain the
  configured Codex host access. This deployment has one owner.
- Human members in `PUBLIC_CHANNEL_IDS`, or in every channel when
  `PUBLIC_ACCESS_ALL_CHANNELS=true`, may invoke exact `Jarvis` triggers,
  mentions, and direct replies (even with Discord's reply ping off) for factual
  Q&A, web lookup, technical and coding
  questions, image generation and editing, how-to guidance, and server-history questions. Public memory is
  isolated per member and channel.
- Public requests cannot use shell commands, apps, local-file tools,
  private-network access, host writes, account operations, or other actions on anyone's
  behalf. Their Codex turns are ephemeral and read-only; the built-in image tool
  alone may create its generated outputs. Verified PNG/JPEG/WebP outputs from
  that fresh request's thread directory are attached in the same conversation,
  never pre-existing files from another request. Image requests share the public
  quota reserve and timeout. For example: `Jarvis draw a turtle astronaut`.
  Attach or reply to an image to request an edit. This permission is for humans;
  the separate, narrow R exception below is unchanged.
- Public server context is restricted to channels the asking member can already
  view. It excludes soft-deleted messages.
- Public access pauses when the five-hour allowance has 25% or less remaining;
  owner access continues. A shorter prompt limit also constrains public usage.
- `Jarvis usage` is available to everyone in a public channel, consumes no model
  turn, and remains available while public Q&A is paused.
- Incoming Discord bot and webhook messages are context-only. They cannot start
  an AI turn or cause Jarvis to reply, even if they mention Jarvis, reply to him,
  are on a historical conversation allowlist, or legacy bot-access flags are on.
  `Jarvis /bots status` explains this policy; `/bots on` cannot override it.
- Owner-authorised exception: R (`900000000000000001`) may explicitly address
  Jarvis with an image-generation request in an enabled public server channel.
  This uses an ephemeral image-only model turn with shell, apps, MCP, computer,
  account and general host operations disabled. Valid generated raster files
  under the configured Codex generated-images directory are attached in the same
  channel. DMs, webhooks, other bots, ordinary R chatter, and replies to Jarvis's
  fallback messages do not qualify. These jobs cannot delegate back to R and
  remain subject to the existing usage reserve. No general bot chat is enabled.
- When a human's shareable request cannot be completed, Jarvis can ask R
  (`900000000000000001`) to try in the same server channel. A final-line
  `[[JARVIS_ASK_R]]` control marker requests delivery by the application; the model
  never claims R completed the task. CLI failures also attempt this fallback for
  eligible requests. Only the original request and a source-message link are
  forwarded, not ambient context or errors. Handoffs use a short friendly ping,
  the requester and a linked source, a small scope note, and the original request
  in a Discord block quote. Credential-bearing, private-network,
  host/account-operation, overlong, bot-originated and DM requests are excluded.
  Each source message gets one attempt and only R can be pinged. A SQLite
  transaction reserves a slot before sending, enforcing a global maximum of
  **three attempts per UTC calendar day**, shared across every user and channel
  and surviving restarts. Failed/ambiguous sends still consume a slot; storage
  failures block delivery. Only content-free source IDs/day/status are retained.
  R's reply is for the human; all incoming bot messages are archived without
  triggering another Jarvis turn, so no automatic bot-to-bot conversation starts.
- Codex uses its configured access mode. `danger-full-access` allows it to read
  and write outside the working directory, so authorize only trusted accounts.
- Jarvis must never restart its own systemd service synchronously during a
  Discord request. `scripts/restart-after-reply` queues a worker that waits for
  the active Codex turn and its completion reply to finish before restarting.
- Every Codex turn writes content-free request telemetry as JSONL. Records include
  mode, correlation IDs, model attribution when available, provider-reported token
  counts, latency, and outcome. Prompts and responses are never written there.
- Active Discord-triggered Codex turns also publish a redacted live progress
  snapshot. From the project directory, run `./jarvis-watch` for the current
  state or `./jarvis-watch -f -v` to follow tool activity and sanitised shell
  commands. Prompts, replies, and command output are never included.
- Set `PROGRESS_CHANNEL_ID` to mirror that verbose live view into a Discord
  channel. Jarvis posts one dashboard per busy period and edits it as work
  advances, avoiding a new message for every tool event.

## Incoming DM copies

Set `DM_COPY_OWNER_ID` to an ID in `AUTHORIZED_USER_IDS` to copy each new incoming
human DM to that owner's DM inbox. Copies include the sender's username and ID,
timestamp, full text, reply message ID when present, attachment links, and stickers.
Plain replies and messages without a Jarvis trigger are copied too, before channel
and invocation checks. This does not grant senders any additional bot access.
The recipient's own DMs, server messages, bots, and webhooks are excluded.
Mentions and link previews are disabled in copies. Calendar feed secrets, raw
calendar text, and calendar attachments are withheld. Attachments are linked
without downloading or opening them; Discord attachment links can expire.
Copies start when enabled; there is no historical backfill or offline delivery
queue. Delivery failures are logged using IDs and error types only and do not
prevent normal request processing. The owner must allow DMs from the bot.

## One-off DM reply notifications

An explicitly authorised reply watch can run independently of the bot using
`python -m jarvis_bot.dm_reply_watch <private-state-file>` under a user service.
It checks the specified DM and recipient once a minute, starting after the exact
outgoing invitation. The single alert goes to the authorised channel and mentions
only its requesting owner. Bot messages and unrelated authors do not trigger it.
It never forwards reply contents or invokes AI. State holds identifiers/status
only; credentials are read privately at runtime. Successful notification is checked
through Discord and reconciled after an interrupted send before retrying.
The watch exits after notification and survives bot restarts. To cancel a user
service watch, disable and stop that watch's unit; do not restart the main bot.

## Work schedule and public availability

`Jarvis schedule`, `Jarvis show my schedule today`, `Jarvis schedule tomorrow`,
`Jarvis schedule YYYY-MM-DD`, `Jarvis schedule this week`, and
`Jarvis schedule next week` read a published Outlook ICS availability feed.
Since 2026-11-04, `sch` also works as shorthand: `Jarvis my sch`,
`Jarvis my sch tomorrow`, and `Jarvis my sch next week` use the same
timezone-aware date selection and anonymous reports as `schedule`, including
the side-by-side week timeline. The shorthand accepts the same date qualifiers
and polite request forms; unrelated words such as `school` do not match.
Since 2026-11-06, named weekdays also work: `Jarvis my sch Friday`,
`Jarvis my sch on Friday`, and `Jarvis my sch next Monday` select a single day
using the configured calendar timezone. A bare weekday selects its next occurrence,
including today. `this Friday` selects Friday in the current Monday–Sunday week
(even if it has passed); `next Friday` selects Friday in the following week,
consistent with `this week` and `next week`. Full weekday names, including weekends,
work with the existing schedule/calendar/meetings requests and polite wrappers.
The selected day uses the same anonymous agenda, timeline, huddles, exact open
windows, overlap totals and fetch-time display as an explicit date request.
By default only the configured `WORK_CALENDAR_OWNER_ID`, also present in
`AUTHORIZED_USER_IDS`, can request it. With the owner's explicit opt-in,
`WORK_CALENDAR_PUBLIC_ENABLED=true` also lets human public users request that same
calendar's anonymous availability in DMs and enabled public server channels.
Use `Jarvis schedule`, `Jarvis schedule tomorrow` or `Jarvis schedule this week`.
These deterministic reports include the same timeline and require no AI quota;
they do not grant calendar-editing, host or other owner-only access. Bots and
webhooks remain unable to invoke commands. Replies identify the calendar owner,
never expose event details or credentials, and are visible to everyone who can
read the requesting channel. Set the flag to `false` to return to owner-only access.

Set `WORK_CALENDAR_ICS_URL` privately in `.env`, along with
`WORK_CALENDAR_OWNER_ID`, `WORK_CALENDAR_TIMEZONE` (default `America/Chicago`),
`WORK_DAY_START` (default `08:30`), and `WORK_DAY_END` (default `16:00`).
Publish Outlook with availability-only permissions. Anyone possessing the feed
URL can read the published availability. Never disclose the feed URL, its token,
or raw ICS to anyone, including in DMs, attachments, handoffs, model prompts,
logs, examples, or source control. Only anonymous availability is shown, to the
configured owner or public users when opted in. The adapter accepts HTTPS feeds from
`outlook.office365.com` and does not follow redirects.

Each request fetches the feed anew. Raw ICS is held in memory only; titles,
attendees, descriptions, locations and attachments are discarded before recurrence
expansion. Numbered timings and totals are rendered directly without sending the
feed or event details to Codex. The bot's normal channel history may retain the
anonymous schedule replies. Errors never include feed URLs or parser content.

Single-day requests use a vertical busy/free timeline. Multi-day requests end
with one side-by-side comparison in the week summary, with a column for each
day, dates, per-day timezone abbreviations, and the fetch time. Both views use
15-minute blocks in 30-minute rows, distinguishing partially busy cells from
fully busy or free cells. Weekends are marked as having no work hours rather
than shown as available work time. Each day retains its anonymous agenda, bold
time ranges, scheduled/available summary, and exact open windows; overlaps and
morning huddles use the same busy intervals in the comparison and totals.
Long comparison grids repeat column headers and keep complete fenced rows
within Discord's message limit; ranges longer than five days use groups of up
to five columns. This comparison was implemented on 2026-11-03.
Long agendas split
at complete rows to stay within Discord's message limit. Recurrence, exceptions and time zones are
handled by iCalendar libraries. Cancelled and free events are excluded.
Since 2026-11-05, cancelled recurrence exceptions that omit `DTSTART` use their
`RECURRENCE-ID` to remove only the matching occurrence instead of failing the
whole report. This supports timed and all-day exceptions, including UTC identifiers
for local-time series across daylight-saving changes.
Meeting totals merge overlaps and count only time inside Monday–Friday work hours;
timed events outside work hours are still listed. All-day busy entries are flagged
but excluded from meeting totals. Holidays and lunch are not deducted. Busy
appointments such as focus blocks also appear as numbered meetings. Outlook
publishing may lag behind calendar edits; the response shows the fetch time.

The owner's recurring weekday 12:30–1:30 PM lunch counts as available.
`WORK_CALENDAR_FREE_SERIES_SHA256` privately identifies that specific series by
its SHA-256 UID fingerprint. Only its exact 12:30–1:30 PM occurrences in the
configured time zone are excluded; separate overlapping meetings and moved or
extended occurrences still block their actual intervals. This changes Jarvis's
availability calculation without editing Outlook. Leave the setting empty to
disable the override; never store raw event IDs in this setting.

Owner-authorised schedule shares and booking invitations, including DMs, must
reuse fresh `render_schedule` output with the configured display settings. Keep
the visual timeline and legend, anonymous agenda, open windows and totals rather
than replacing them with a plain availability list. Preserve complete pages when
splitting long messages. This presentation preference does not grant calendar
access, authorise unsolicited sharing, or expose feed credentials or event details.

Set `WORK_MORNING_HUDDLE_ENABLED=true` to reserve 8:30–9:30 AM in the configured
time zone every Monday–Friday, whether or not Outlook includes it. This block is
explicitly labelled `Morning Huddle`; other entries retain anonymous meeting
labels. It is included in the agenda and meeting totals, with overlaps
counted once. An identical Outlook block is represented by the huddle row rather
than duplicated. This only affects Jarvis's schedule display; it does not edit
Outlook or set a Discord presence status.

The container uses `TZ` from `WORK_CALENDAR_TIMEZONE` instead of binding the host's
`/etc/localtime`. Binding over a symlink can replace the image's UTC zone file and
make UTC calendar entries resolve incorrectly. Named UTC and Central zones must
retain their distinct offsets.

## 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, and open its **Bot** page.
2. Create/reset the bot token and keep it secret.
3. Under **Privileged Gateway Intents**, enable **Message Content Intent**.
4. On **Installation**, enable Guild Install with the `bot` scope. Grant only
   **View Channels**, **Send Messages**, and **Read Message History**.
5. Use the generated install link to add the bot to your server.

Changing the bot's own avatar does not require an additional guild permission.
The avatar is global across every server that uses the bot, and Discord may rate-limit
frequent changes.

## 2. Sign in to Codex

Install Codex CLI and sign in with the ChatGPT account attached to your
subscription:

```bash
codex login
codex login status
```

The status should say `Logged in using ChatGPT`.

## 3. Configure and run

```bash
cd /home/your-user/Projects/jarvis-discord-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and provide the Discord token and authorized Discord user ID:

```dotenv
DISCORD_BOT_TOKEN=your_discord_bot_token
AUTHORIZED_USER_IDS=your_discord_user_id
```

Then run:

```bash
python main.py
```

Do not paste the Discord token into Discord or commit `.env` to source control.

### Docker deployment

The Docker deployment keeps durable state in the host `data/` directory and
uses the existing host `.env` file. It also bind-mounts `/home/your-user` so the
Codex CLI keeps its existing ChatGPT login, session history, and project paths.
The independent watchdog and scheduled reminder/countdown units continue to run
as host user services.

The watchdog checks the local heartbeat and Discord's public gateway endpoint
every 30 seconds. Three consecutive failures record an outage; two successful
checks record recovery. These observations are saved before notification
delivery. If internet access prevents posting, the GIF notifications remain in
a private on-disk queue and are delivered in order when Discord is reachable,
including after a watchdog restart. Notifications include their observation
times so a delayed offline GIF is identifiable as a recorded outage. Brief
interruptions below the failure threshold may produce no alert. A watchdog on
the same connection cannot announce a total internet outage in real time.

Status notices use matching animated HUD cards: cyan rotating-core recovery and
an amber/red broken link for outages. Captions carry Discord local/relative
timestamps from the recorded observation, not the later delivery time. The
original graphics are reproducible with the build-time Pillow script
`scripts/render-watchdog-notifications.py --preview-dir outputs/watchdog-preview`;
the watchdog only reads `assets/jarvis-{online,offline}-hud.gif` and has no new
runtime dependency. Previous GIFs/source art remain available in `assets/`.
Restart only `jarvis-watchdog.service` to reload presentation changes; the main
Discord bot and its reply path stay running.

The container runs as UID/GID 1000, has a read-only root filesystem, drops all
Linux capabilities, and restarts unless explicitly stopped. The host
`/srv/storage` mount remains available for the existing download/media workflow;
its host `noexec,nosuid,nodev` controls must remain in place.

Build and validate the image before stopping the host process:

```bash
docker compose build
docker compose run --rm --no-deps jarvis python -c \
  'from jarvis_bot.config import Settings; Settings.from_env(); print("settings ok")'
docker compose run --rm --no-deps jarvis codex login status
```

For the cutover, install `scripts/systemd/jarvis-discord-container.service` as
`~/.config/systemd/user/jarvis-discord.service`, reload the user manager, and
restart that unit. Keep a copy of the original unit for rollback. Do not stop
`jarvis-watchdog.service` during deployment.

From an active Discord request, use `scripts/restart-after-reply` instead of
restarting synchronously. Its Docker process check includes the required PID
column and waits when inspection fails. Each Discord message handler holds a
shared restart lock through its final reply; the worker requires the exclusive
lock before restarting. CLI completion alone does not mean the reply was sent.

Useful checks after cutover:

```bash
docker compose ps
docker compose logs --tail=100 jarvis
systemctl --user is-active jarvis-watchdog.service
sqlite3 data/jarvis.sqlite3 'PRAGMA quick_check;'
```

## Container reports

`jarvis-container-status.service` runs a standard-library Python collector on
the host every 30 seconds. It uses `scripts/docker-cli` for Docker list, inspect,
one-shot stats and fixed read-only VPN exec probes. Jarvis reads `data/container-status.json` through its
existing data mount; it needs no Docker socket, new network listener or token.
The report includes stopped containers and discovers all Compose projects.
`data/expected-containers.json` lists essential container names so removal is
reported as missing. Update that list after intentional renames or removals.

Only selected identity/state/health/resource fields are published. Environment
variables, mount paths, raw health output and logs are excluded. Reports are
atomically replaced with mode 0600. A failed daemon query publishes an explicit
unavailable result; reports older than 90 seconds are labelled stale. Resource
sampling failure does not erase a successful inventory. Docker health checks do
not independently establish app functionality or VPN leak protection.

The report begins with separate qBittorrent VPN/protection evidence: an HTTPS
request from qBittorrent to Mullvad's connection-check endpoint, the actual
shared network namespace, saved `tun0` binding, and strict IPv4/IPv6 OUTPUT rule
inspection. Rules must have default DROP and only the recognized local,
established, tunnel, IPv6 discovery and configured WireGuard endpoint exceptions.
Unexpected allow rules fail validation. No IP addresses, provider account data,
full preferences or WireGuard keys are published in the report.

A fresh IPv4 HTTPS connection to a fixed numeric target through `tun0` is used
as a positive control before attempting the same connection forced through
`eth0`. Only a timeout after the positive control passes counts as supporting
evidence of blocked bypass. Invalid interfaces, missing tools, DNS-only failures,
unavailable controls and inaccessible checks never produce a passing result.
Container identity/start-time changes invalidate the sample. No download,
route, firewall or container state is changed. A full tunnel-outage test is
explicitly marked **not performed**; these probes do not prove every failure
scenario and do not automatically pause or restart qBittorrent.

Install `scripts/systemd/jarvis-container-status.service` into
`~/.config/systemd/user/`, reload the user manager, and enable/start that unit.
It requires the host account's existing Docker access, but no Discord credentials.
Check with `systemctl --user status jarvis-container-status.service` and
`python -c 'from jarvis_bot.container_status import read_container_report; print(read_container_report())'`.
To undo, disable/stop only the collector unit and remove its installed unit file;
the command will label the last report stale. Rebuild the Jarvis image to deploy
command changes, then use `scripts/restart-after-reply` during Discord requests.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `JARVIS_TRIGGER` | `Jarvis` | Name accepted anywhere in a message; `jar`, close typos, mentions, and replies also work |
| `MEMORY_DATABASE_PATH` | `./data/jarvis.sqlite3` | SQLite database location |
| `SERVER_IDENTITIES_PATH` | `./data/server-identities.json` | Durable server-wide name-equivalence reference |
| `MEMORY_MAX_STORED_MESSAGES` | `200` | Stored messages retained per user/channel |
| `CHAT_HISTORY_BACKFILL_LIMIT` | `1000` | Previous messages fetched per accessible channel at startup; `0` means all history |
| `CHAT_CONTEXT_MESSAGES` | `50` | Recent channel messages supplied to each Jarvis turn |
| `CHAT_CONTEXT_MAX_CHARS` | `24000` | Character limit for supplied channel context |
| `CHAT_MAX_STORED_MESSAGES_PER_CHANNEL` | `10000` | Legacy compatibility setting; channel records are no longer pruned |
| `ALLOWED_CHANNEL_IDS` | empty | Optional comma-separated channel allowlist |
| `AUTHORIZED_USER_IDS` | required | Discord accounts allowed to command Codex |
| `PUBLIC_CHANNEL_IDS` | empty | Channels where non-owner human members may use restricted Q&A |
| `PUBLIC_ACCESS_ALL_CHANNELS` | `false` | Enable restricted public Q&A in every bot-accessible channel |
| `PUBLIC_SETUP_GUIDANCE_USER_IDS` | empty | Legacy compatibility setting; guidance is available to all public users |
| `PUBLIC_MIN_FIVE_HOUR_REMAINING_PERCENT` | `25` | Public access pauses at or below this five-hour reserve |
| `PUBLIC_MAX_PROMPT_CHARS` | `2000` | Maximum public prompt length |
| `PUBLIC_MEMORY_MESSAGES` | `20` | Persisted public conversation messages supplied to a turn |
| `PUBLIC_GUILD_CONTEXT_MESSAGES` | `50` | Visible server-history messages supplied to a public turn |
| `BOT_INVOCATIONS_ENABLED` | `false` | Legacy setting; owner policy now keeps all incoming bots context-only |
| `BOT_MIN_FIVE_HOUR_REMAINING_PERCENT` | `50` | Legacy setting; incoming bot invocations are now disabled |
| `BOT_CONVERSATION_USER_IDS` | empty | Legacy setting; cannot bypass the context-only bot boundary |
| `R_PREFIX_USER_IDS` | empty | User IDs whose normal replies receive the configured R-style prefix; an exact terminal `ADJOURN` remains undecorated |
| `CODEX_BINARY` | `codex` | Path to the Codex CLI executable |
| `CODEX_WORKDIR` | current directory | Directory Codex may work within |
| `CODEX_SANDBOX` | `workspace-write` | `read-only`, `workspace-write`, or `danger-full-access` |
| `CODEX_TIMEOUT_SECONDS` | `900` | Maximum time for one Codex turn |
| `CODEX_MODEL` | empty | Optional explicit Codex model; setting it makes per-request model attribution exact |
| `CODEX_REASONING_EFFORT` | empty | Optional explicit Codex reasoning effort applied to all request modes |
| `MAX_PROMPT_CHARS` | `12000` | Maximum accepted Discord prompt length |
| `TELEMETRY_PATH` | `./data/request-telemetry.jsonl` | Append-only per-request JSONL telemetry |
| `PROGRESS_PATH` | `./data/live-progress.json` | Atomic, redacted snapshot read by `./jarvis-watch` |
| `PROGRESS_CHANNEL_ID` | empty | Optional Discord channel receiving the live verbose progress dashboard |
| `VOICE_TRANSCRIPTION_ENABLED` | `true` | Automatically transcribe new Discord voice notes locally |
| `VOICE_TRANSCRIPTION_CHANNEL_IDS` | empty | Optional comma-separated channel allowlist for voice notes |
| `VOICE_TRANSCRIPTION_MODEL` | `base.en` | Local faster-whisper model (downloaded on first use) |
| `VOICE_TRANSCRIPTION_DEVICE` | `cpu` | faster-whisper inference device |
| `VOICE_TRANSCRIPTION_COMPUTE_TYPE` | `int8` | faster-whisper compute type |
| `VOICE_TRANSCRIPTION_MAX_BYTES` | `20971520` | Maximum downloaded voice-note size |
| `VOICE_TRANSCRIPTION_MAX_SECONDS` | `300` | Maximum voice-note duration |
| `LIVE_VOICE_ENABLED` | `false` | Enable persistent capture and local transcription of one VC |
| `LIVE_VOICE_CHANNEL_ID` | empty | Voice channel to monitor |
| `LIVE_VOICE_TEXT_CHANNEL_ID` | empty | Text channel for listening notices and spoken-request answers |
| `LIVE_VOICE_WAKE_PHRASE` | `Hey Jarvis` | Exact wake phrase searched in transcripts (minor punctuation is ignored) |
| `LIVE_VOICE_SILENCE_SECONDS` | `1.2` | Silence used to close an utterance |
| `LIVE_VOICE_SEGMENT_MAX_SECONDS` | `30` | Maximum audio duration per transcription segment |
| `LIVE_VOICE_MIN_SPEECH_SECONDS` | `0.35` | Minimum voiced audio retained as a segment |
| `LIVE_VOICE_RMS_THRESHOLD` | `250` | PCM energy threshold used to reject silence |
| `LIVE_VOICE_FOLLOWUP_SECONDS` | `10` | Same-speaker request window after saying only the wake phrase |
| `LIVE_VOICE_REQUEST_COOLDOWN_SECONDS` | `10` | Per-member spoken-request cooldown |
| `LIVE_VOICE_RETENTION_DAYS` | `0` | Transcript retention; `0` keeps notes until manual deletion |
| `LIVE_VOICE_SUMMARY_MAX_MINUTES` | `10080` | Largest permitted bounded summary or timed-deletion window |
| `LIVE_VOICE_REQUESTS_READ_ONLY` | `true` | Require text confirmation for actions requested in voice |

## Memory and privacy

The bot sends triggered prompts plus recent channel context to the locally
installed Codex CLI. SQLite maps each Discord user/channel to a persistent Codex
session and stores channel messages in `data/jarvis.sqlite3`, including server,
channel, author, message, and timestamp metadata. Ambient messages are labelled
as untrusted reference data so they cannot authorize laptop actions. Anyone with
filesystem access to the database can read the stored conversations, so protect
the host and backups. Tell server members that their messages are being retained.

Voice-note audio is written only to a temporary file for local transcription and
then discarded. Its transcript is posted to Discord and retained as channel
context. Inform channel members accordingly; spoken commands are still authorized
by the original Discord account, never by the words in the recording alone.

Live VC audio is buffered only in memory until each utterance is transcribed,
then discarded. Timestamped transcripts remain in SQLite until an owner deletes
them. Setting `LIVE_VOICE_RETENTION_DAYS` to a positive number restores automatic
expiry; `0` disables pruning. Anyone entering the monitored channel must receive
clear notice that transcription is active. Summary access is checked against the
member's permission to view that voice channel.

Live reception currently relies on the community
`discord-ext-voice-recv-dave` fork pinned by commit in `requirements.txt`, paired
with discord.py 2.7.1 and `davey`. `/vc-notes status` reports whether the bot is
connected and actively receiving, so a future Discord voice-protocol change is
visible rather than silently losing notes.

Only owner accounts receive the configured host-capable Codex session. When
`CODEX_SANDBOX=danger-full-access`, owners can direct Codex to read, modify, or
delete files outside `CODEX_WORKDIR`; treat the Discord account as an administrator
credential and enable multi-factor authentication. Public turns use a separate
ephemeral command with a read-only sandbox and the shell, local-file, app, browser,
computer-control tools disabled. Hosted web search remains available. Human
public text requests additionally permit the built-in image generation tool;
voice/read-only helper turns do not gain this capability.

## Request telemetry

Inspect a request that is taking a while:

```bash
cd /home/your-user/Projects/jarvis-discord-bot
./jarvis-watch
./jarvis-watch -f -v
```

The default view shows lifecycle events. `-v` also shows sanitised shell command
lines and model-generated reasoning summaries when Codex provides them; neither
mode exposes raw private reasoning, prompts, assistant replies, or command output.
Press Ctrl-C to stop follow mode.

Live progress distinguishes a finished work item from a successful operation.
Completed items with an explicit `failed` status are labelled as failures;
shell commands with a nonzero integer exit code show `Shell command failed`
with that exit code, even if the item's status is `completed`. The local watcher
and Discord progress renderer use these labels without including raw error
messages, tool results or command output. Command redaction still applies.
An individual tool failure does not finish the enclosing request: the agent can
recover and ultimately succeed. Failure labels persist in the snapshot and
survive interruption recovery. Synthetic regression coverage is in
`tests/test_progress_item_failures.py`.

While other requests are still running, the progress heading keeps the latest
finished request's ID, outcome and fixed elapsed time visible. Errors and
cancellations no longer disappear behind concurrent work, and this result stays
visible when Discord truncates a long activity history. Recovered interruptions
use their existing lower-bound duration; individual tool failures still do not
finish a request. Only the latest result is shown alongside active work, without
adding that finished request's event details. Synthetic lifecycle, truncation and
restart regressions are in `tests/test_progress_concurrent_results.py`.

Progress writes remain serialized when a request times out or is cancelled:
Jarvis waits for the in-flight file write before releasing its progress lock,
then propagates cancellation. This lets the final outcome and subsequent requests
publish safely without competing for the same temporary snapshot file. Viewers
are notified after a successful interrupted write; disk failures are logged while
preserving cancellation. This serialization protects in-process interruptions.

On startup, Jarvis recovers the previous progress snapshot before publishing its
new state. Previously active requests are marked `interrupted`, with their
request IDs, session IDs and bounded redacted event histories retained in an
`interrupted` list. The most recently accepted interrupted request is also shown
as the last result. The local watcher and Discord progress renderer show these
interruptions explicitly; execution is not automatically resumed. Elapsed time
is a lower bound through the last observed activity, excluding downtime, and no
completion time is invented. The latest recovered batch survives further
restarts and remains visible while new requests run normally. If no work was
active, the last known completion outcome is preserved instead of being erased.
Unreadable or malformed snapshots generate a recovery warning without preventing
new work; the local watcher reports unavailable state with an unknown active
count when it cannot read the snapshot. Synthetic regression coverage is in
`tests/test_progress_restart.py`.

Restart recovery also salvages valid requests from a partially damaged snapshot.
A malformed request entry or top-level request field no longer discards valid
concurrent requests, earlier interruptions or a known completion in other fields.
Invalid entries are skipped with a structural warning that excludes their
contents. Before recovery publishes a clean snapshot, the local watcher still
reports unavailable state and an unknown active count. Recovered unfinished work
keeps its observed duration and is marked interrupted without resuming execution;
unparseable JSON still follows the existing unavailable-state handling. Synthetic
mixed-validity, repeat-restart and failed-write regressions are in
`tests/test_progress_partial_recovery.py`.

If a request's initial progress snapshot fails to write, registration now rolls
back before the error reaches the assistant. The request can still run using the
existing error handling, but later events cannot resurrect it as permanently
active when completion tracking was never enabled. Previously active requests
and the last completed result remain intact, and later requests can publish
normally once storage recovers. A failed replacement using the same request ID
also preserves that request's prior session and events. Regression coverage in
`tests/test_progress_start_failure.py` injects atomic-write failures and exercises
assistant success, failure and cancellation followed by a successful request.

Cancellation during that initial snapshot write also closes registration safely.
Because the assistant has not yet enabled its completion cleanup, the progress
store records a `cancelled` result itself before propagating cancellation. It
waits for both writes under the progress lock and notifies viewers of the final
state, preventing an abandoned request from remaining active indefinitely.
Other active requests are preserved; cancelling a same-ID replacement restores
the original request's session and events. Repeated cancellation preserves the
first cancellation reason. If the cleanup write fails, the failure is logged,
the in-memory result remains cancelled, and the next successful progress write
publishes the corrected state. This does not resume execution. Synthetic
regressions, including the assistant registration path and restart recovery,
are in `tests/test_progress_start_cancellation.py`.

Final progress snapshots retry once if an ordinary filesystem error prevents
publication. This lets a transient write or atomic-replace failure recover
without another request arriving: a finished request is removed from the active
list and its actual success, error or cancellation outcome survives restart.
The retry keeps the original completion timestamp and event history, holds the
existing progress lock, and notifies viewers only after a successful write.
Cancellation still waits for its in-flight writer and propagates normally.
If both attempts fail, the error reaches the existing caller error handling;
the previous disk snapshot remains stale until a later successful publication.
Synthetic regression coverage is in `tests/test_progress_completion_retry.py`.

Completion cleanup also survives cancellation while waiting for the progress
lock behind another update. It keeps its place in the queue, removes the finished
request from the active list, and publishes the supplied success, error or
cancellation outcome before propagating the first cancellation reason. Repeated
cancellation during the wait or final write cannot abandon that cleanup. Other
active requests and the completed request's session and event history remain
intact. Filesystem failures still get at most two write attempts; if both fail,
the failure is logged and cancellation propagates, with the terminal state kept
in memory for the next successful publication. This handles cancellation after
`finish()` has started, not process termination. Synthetic contention, repeated
cancellation, restart and disk-failure regressions are in
`tests/test_progress_queued_completion.py`.

Live progress now follows the agent's task checklist through `item.started`,
`item.updated` and `item.completed`, showing agent-reported counts such as
`Task checklist: 1/3 complete`. Closing a checklist item does not claim its
unchecked tasks are complete, and even a fully checked list leaves the request
active until normal completion cleanup supplies its actual outcome. Only counts
are retained; task descriptions are neither persisted nor displayed. Empty lists
are labelled empty, and malformed updates show unavailable progress instead of
retaining stale counts. An explicitly failed update preserves the last observed
counts and adds a failure label.

The latest counts are kept separately from the bounded activity history, survive
interruption recovery, and appear below each displayed request's activity. For a
single request, this footer keeps current counts visible when Discord truncates
long activity details. This reports checklist state, not independently verified
work or automatic execution recovery. Synthetic lifecycle, privacy, malformed
input, concurrency, truncation and restart regressions are in
`tests/test_progress_checklists.py`. The checklist event shape follows the
[official Codex item types](https://github.com/openai/codex/blob/main/sdk/typescript/src/items.ts).

Once request telemetry recording starts, cancellation waits for the queued or
in-flight append to finish before propagating the first cancellation reason.
The write lock stays held until the worker finishes, preventing later records
from racing an interrupted JSONL append. Each record keeps its supplied outcome
and is attempted once; append failures are not retried because a partial write
may already have reached disk. Ordinary write failures reach the caller. If a
write fails during cancellation, the failure is logged and cancellation still
propagates; subsequent records can proceed. This protects in-process cancellation,
not abrupt process termination or writes from separate store instances. Synthetic
regressions are in `tests/test_telemetry_cancellation.py`.

Aggregate completed-request telemetry by UTC date, request mode, and model:

```bash
python -m jarvis_bot.telemetry data/request-telemetry.jsonl
```

The aggregator reports request outcomes, token categories, cache rate, total
tokens, and average latency. Estimated cost remains `null` until a versioned
pricing source is configured; this deployment uses the Codex CLI's ChatGPT login,
so API-style dollar billing must not be inferred from token counts alone.

## Owner request duration

`CODEX_TIMEOUT_SECONDS` remains the normal overall Codex request limit (900
seconds by default). Owners listed in `CODEX_UNLIMITED_TIMEOUT_USER_IDS` have no
overall timeout on their human requests, including DMs and read-only voice
requests. The list must be a subset of `AUTHORIZED_USER_IDS`; being mentioned in
a prompt never grants an exemption. Other users and bot requests keep the normal
deadline. Cancellation, process-error cleanup, short network/tool deadlines and
the separate nightly maintenance runtime/quota limits still apply. An unlimited
request can keep its conversation session occupied until it completes or stops.

## Images shared in chat

Human requests can include uploaded images, direct HTTPS image links, and images
in Discord link previews. Reply to an image message with a question, or mention
Jarvis alongside the image. A mention with only an image asks for a description.
An explicit visual reference such as "what is in that picture?" can use the
nearest image among eight preceding messages in the same channel or DM.
The bot never scans other channels for image inputs or responds to every image
automatically. Existing owner/public permissions and usage gates still apply;
bot/webhook messages stay context-only under the existing narrow exceptions.

Up to four images are downloaded per request, at most 8 MiB and 20 million pixels
each. PNG, JPEG, WebP and GIF are decoded and normalised to metadata-free PNGs
with a maximum 2048-pixel edge. GIFs supply their first frame, not motion/video.
Only public HTTPS endpoints are fetched: DNS results and every redirect are
checked against private/local addresses, with no cookies, credentials or proxy
inheritance. Stream limits and timeouts bound downloads. HTML pages, SVGs, private
links, oversized or corrupt files produce an unavailable-image note rather than
invented visual observations; ordinary webpages without an image preview are
not scraped for images. Discord's signed attachment links must still be valid.

Images are passed as actual Codex `--image` inputs for new and resumed owner
sessions and ephemeral public questions. Text inside images remains untrusted
reference material and grants no authority. Public tools remain restricted.
Temporary files have private permissions and are removed after completion,
failure or cancellation; normal Codex session retention still applies to inputs.
Pillow is a pinned runtime dependency. Protocol reference:
[official OpenAI image input documentation](https://learn.chatgpt.com/docs/image-inputs?surface=cli).

## Free Codex reset expiry warnings

`python -m jarvis_bot.reset_credit_watch --owner-id 900000000000000003`
reads earned resets using Codex's authenticated `account/rateLimits/read` RPC.
It runs no model turn, spends no reset, and never invokes a redemption RPC.
Available resets with a known future expiry trigger owner-only Discord DMs at
72, 24 and 6 hours remaining. An hourly check can run up to about an hour after
a threshold; starting inside several thresholds sends only the most urgent one.
Dates use America/Chicago, including daylight saving time.

`--inspect` reads the live balance and expiry dates without sending messages or
changing state. Missing balance/details are not interpreted as zero or replaced
with cached dates. The service may cap detail rows, so availableCount remains
authoritative; a reset without a returned expiry cannot receive a timed warning.
Each normal run fetches fresh data, so redeemed or removed credits stop alerting.

Private state in `~/.local/share/jarvis-reset-watch/state.json` records delivered
warning bands across restarts. A process lock prevents overlapping checks. Failed
lookups or DMs fail the service and retry on the next hourly run; they never fall
back to a public channel. The recipient is verified as a one-to-one owner DM.
Discord receipt checks precede marking alerts sent; stable message nonces reduce
duplicates on immediate retries. A crash after delivery but before saving state
can still duplicate a reminder beyond Discord's nonce deduplication window.

Install `scripts/systemd/jarvis-reset-credit-watch.{service,timer}` under
`~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then
`systemctl --user enable --now jarvis-reset-credit-watch.timer` and start the
service once to verify live delivery. The persistent hourly timer catches up
after downtime. This host-side checker needs no Discord bot restart or image
rebuild; stop/disable its timer to stop future checks. A blocked DM or an offline
host prevents delivery; verify Discord/iPhone notification settings separately.

Protocol reference: [official OpenAI App Server documentation](https://learn.chatgpt.com/docs/app-server#6-rate-limits-chatgpt).

## Overnight improvements

The owner-authorised nightly job runs at **02:00 America/Chicago** (Central local
time, including daylight saving changes). It targets one concrete functional
improvement per run; it cannot honestly guarantee a deployment when quota,
infrastructure or validation fails. Failed runs are reported as failures.
The timer has no daytime catch-up: OMEN must be awake and online at the scheduled
time. A separate supervisor runs every minute and survives a bot restart.

The verified golden snapshot lives under
`~/.local/share/jarvis-maintenance/golden-20261103T181719Z/`. It contains the source
archive, exported Docker image, SHA-256 manifest, private configuration, and one
SQLite online backup with a successful integrity check. It excludes Codex/SSH
authentication, media, virtual environments and generated images. Directory mode
0500 and file mode 0400 restrict accidental access/overwrite; this is a local
recovery copy on the same disk, not off-device disaster recovery or encryption.
Never post its private contents. The database is backed up once in this golden
snapshot; nightly checkpoints contain source and a pinned previous image only.
Automatic code rollback never rewinds the live database or messages.

The trusted controller, policy and prompts are installed outside the source tree
at `~/.local/lib/jarvis-maintenance/`. The editing container receives a clean code
copy and a temporary copy of Codex's own login. It receives no Discord token,
calendar feed, database, host home, Docker socket, or systemd bus. Apps, plugins,
hooks, computer/browser/image tools and delegation are disabled. Nested Linux
sandboxes are unavailable on this host; Docker's restricted mounts, UID, dropped
capabilities, read-only root and resource limits provide the OS boundary. The
independent review container additionally mounts source read-only. Temporary
Codex authentication is removed on normal completion and service cleanup.

As directed by the owner on 6 November 2026, nightly work excludes all schedule,
calendar and availability improvements. The controller rejects calendar source
edits and schedule-related new module/test names; worker and reviewer instructions
also reject that topic under other filenames or bundled with unrelated changes.
Existing schedule behaviour remains available and covered by its tests.
Automatic editing covers progress and telemetry, new non-schedule feature helpers,
new regression test files and README documentation. The next priority is accurate
live task progress and recovery after interruptions, with telemetry accuracy and
useful error handling as alternatives.
Existing tests, auth/routing/configuration, dependencies, database/schema,
deployment and the controller itself are protected. New helper modules must have
reachable useful behaviour. Every accepted patch needs a functional production
change and a new meaningful regression test. Larger work can progress in tested
milestones within this scope; protected integration changes require owner direction.

Each run verifies the golden file hashes and database integrity, checkpoints
source/image, tests the baseline, implements in isolation,
checks the protected-file diff, runs old and new tests, obtains an independent
read-only review, builds from the exact previous runtime image, and tests installed
candidate code. Publication rejects concurrent source changes.

Failure reports identify the fixed, human-readable stage (including a baseline
test failure before implementation); subprocess logs and assertion contents stay
private. Baseline, candidate and packaged-image test failures have distinct
reason codes. Tests that require workday output use fixed weekday fixtures so
weekend maintenance runs are not mistaken for application regressions.
Startup prerequisite failures also create a durable report before the worker
starts, including missing Codex executables, unavailable policy and missing
backup pointers. Configure `codex_binary` with the stable
`/home/your-user/.local/bin/codex` launcher rather than a versioned release path. The
controller resolves it once per run so the worker mounts the matching executable
and sibling helpers; future runs follow launcher updates.

Restarts wait
for active owner Codex requests using `scripts/restart-after-reply`. The supervisor
announces deployment only after the candidate image passes live Docker health
checks continuously across two observations at least 60 seconds apart. Failure
restores the previous image and our source changes without overwriting subsequent
human edits. Restart/publication timeouts are reported without claiming success.
Interrupted rollback restarts are retried. Private per-run progress and test logs
live under `~/.local/share/jarvis-maintenance/runs/`. Retention preserves the latest
14 runs, the latest three successful versions and the recovery record's run;
older completed runs are removed only after their Discord report is confirmed.
Only this controller's own image tags are removed, never unrelated resources.

The total worker budget is 45 minutes. Initial quota thresholds are 40% remaining
in the five-hour window and 20% weekly. During model work, quota is checked every
minute and stopped at the 25% five-hour reserve or 20% weekly threshold. Provider
reporting delay and work already in flight mean these are stopping thresholds,
not a guaranteed exact quota cap. No paid API fallback is configured.
The installer preserves Jarvis's configured model and reasoning effort while
omitting other host configuration from the worker.

Install/update the trusted copies with the host Python interpreter:
`python scripts/install-nightly-improvement`. The installer does not enable timers.
Validate with `python ~/.local/lib/jarvis-maintenance/nightly.py check`, then enable
`jarvis-nightly-improvement.timer` and `jarvis-nightly-supervisor.timer` using user
systemd. Disable the improvement timer to stop future runs; stop its service to
cancel an active worker. Recovery/reporting remains independent.

The initial setup also replaces a fixed 100ms streaming-test delay with an event
handshake so slower starts do not falsely fail the nightly baseline.
