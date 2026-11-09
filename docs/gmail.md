# Gmail

Read-only import of job-related mail into `EmailMessage` (#12). Nothing is ever sent, deleted or modified.

## Setup (once)

1. Google Cloud project with the Gmail API enabled:
   ```sh
   gcloud projects create <project-id> --name="Job Radar"
   gcloud services enable gmail.googleapis.com --project <project-id>
   ```
2. In the console, **Google Auth Platform** (`https://console.cloud.google.com/auth/overview?project=<project-id>`):
   - *Get started*: app name, support email, audience **External**;
   - *Audience → Test users*: add your Gmail address;
   - *Clients → Create client*: type **Desktop app**, download the JSON.
   - *Audience → Publish app*, see [Publishing the app](#publishing-the-app-193): in *Testing* mode refresh tokens expire after 7 days.
3. Save the JSON as `secrets/gmail_client_secret.json` (git-ignored).
4. Authorize: `uv run job-radar gmail auth`. A browser opens; after consent the token is written to `secrets/gmail_token.json` (mode 600).

Scope: `https://www.googleapis.com/auth/gmail.readonly` only.

## Publishing the app (#193)

In *Testing* mode Google refresh tokens die after 7 days and sync stops with `invalid_grant: Token has been expired or revoked`. A published app keeps its token until you revoke it, change the password, or leave it unused for six months. Publishing a personal app needs no verification; you only see an "unverified app" warning at consent.

Google asks for a homepage and a privacy policy before **Publish app** becomes active. Both live in the public repository [dremdem/job-radar-privacy](https://github.com/dremdem/job-radar-privacy) (GitHub Pages, no code or data):

- homepage: `https://dremdem.github.io/job-radar-privacy/`
- privacy policy: `https://dremdem.github.io/job-radar-privacy/privacy.html`

Steps, in the project's **Google Auth Platform**:

1. **Branding** (`https://console.cloud.google.com/auth/branding?project=<project-id>`):
   - *App name*: `Job Radar`; *User support email*: your address;
   - *App domain*: *Application home page* and *Application privacy policy link* as above (terms of service can stay empty);
   - *Authorized domains*: add `dremdem.github.io`;
   - *Developer contact information*: your address; **Save**.
2. **Audience** (`…/auth/audience?project=<project-id>`): **Publish app** → **Confirm**. The status becomes *In production*. Do not submit for verification.
3. Authorize once more (the old token was issued in Testing): `uv run job-radar gmail auth`. At "Google hasn't verified this app" choose **Advanced → Go to Job Radar (unsafe)** → **Continue**.

## Sync

```sh
uv run job-radar sync gmail                  # all queries, incremental
uv run job-radar sync gmail --query job_proposal
uv run job-radar sync gmail --since 2026-01-01
```

Queries (`job_radar/gmail/queries.py`):

| Name | Gmail query | Stores body | First run looks back |
|---|---|---|---|
| `linkedin_alerts` | `from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)` | yes (HTML + text) | 30 days |
| `job_proposal` | `label:job-proposal` | no | everything |

## How it works

- **Threads, not messages.** Each query is run with `threads.list`, and every message of a matching thread is stored. This is how replies you sent inside a job thread (`direction = out`) are picked up without scanning all of `in:sent`. A wider `in:sent` search for applications outside the label belongs to history backfill (#15).
- **Idempotent.** `EmailMessage.gmail_message_id` is unique; an already stored message only gets the query name added to `categories`.
- **Incremental.** `SyncCursor` keeps, per query, the date of the newest message seen. The next run adds `after:<cursor - 1 day>` to the query; the overlap is harmless because of the unique message id. `--since` overrides the cursor for one run.
- **Quota.** Gmail limits query cost per user per minute (6000 units for a new project). Requests are paced (0.5 s apart); on `429` / `rateLimitExceeded` the client waits 60 s for the next window and retries, up to 5 times. First import of ~1700 threads takes about 15–20 minutes and prints progress every 50 threads.
- **Resumable.** Messages are committed per thread; if a run is interrupted, the next one skips what is stored. The cursor moves only after a query finishes.
- **Direction** is `out` when the message has the `SENT` label, otherwise `in`.

## Rejections

LinkedIn sends an "application update" email from `jobs-noreply@linkedin.com` with
a subject like `Your application to <role> at <company>` and a body that says the
application will not move forward.

`import_rejections` (`job_radar/linkedin/rejections.py`, run after `sync gmail` and
available as `job-radar import rejections`) treats an email as a rejection only when
the subject matches **and** the body contains a rejection phrase. Company and role come
from the subject, the date from the email; a `Rejection` is recorded (`source=gmail`,
see [data-model.md](data-model.md#rejection)). It is idempotent per email id and sets
`EmailMessage.detected_event` and `company_id` for provenance. The job cards in the
email's "Top jobs" section are **not** imported as opportunities: the alert parser only
reads `jobalerts-noreply@linkedin.com`.

What real emails look like (checked against the mailbox):

- The **text/plain part is a stub** ("Your update from <company>" + footer); the
  rejection sentence is only in the **HTML**. Detection reads the HTML text and the
  plain text together.
- Phrases seen: "we will not be moving forward with your application",
  "<company> did not select your application to move forward",
  "we have not selected your application to move forward".
- Employers can write their own text into the same template: "we regret to inform
  you", "decided to proceed with another candidate", "move forward with other
  candidates", or Russian ("К сожалению, мы решили не рассматривать Вашу заявку").
- "Your application was viewed by …" has a different subject and is not a rejection.

**Emails without a stored body.** Only mail found by the `linkedin_alerts` query keeps
its body, and that query looks back 30 days on the first run. Older application
updates reach the database through `label:job-proposal`, headers only. For those,
`import_rejections` fetches the body from Gmail by message id (read-only), runs the
same detection and **stores only the result**, never the body (see privacy decisions
below). A checked email gets `detected_event` = `rejection` or `application_update`,
so it is fetched once; after the rules improve, `job-radar import rejections --recheck`
re-examines the `application_update` ones. Without a Gmail token this step is skipped and the command
says how many emails are waiting.

## Privacy decisions

- Headers (date, from, to, subject), labels and Gmail's `snippet` are stored for every message.
- **Full bodies are stored only for LinkedIn alerts**: they are generated mailings, and the parser (#13) needs their HTML. Private correspondence (`job_proposal`) keeps headers + snippet; if a feature needs a body later, it fetches it by message id and extracts facts without persisting the raw text.
- Mail content is never sent to external AI APIs.
