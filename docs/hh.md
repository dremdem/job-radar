# hh.ru (HeadHunter)

hh.ru sends its notifications from `noreply@hh.ru`. They reach Job Radar through the Gmail label `job-proposal` (see [gmail.md](gmail.md)). As with other private mail, their bodies are **not stored**: the Mail analysis step reads a body from Gmail, keeps what it found and drops the text (issue #99).

## Notifications

Each notice names the vacancy and the employer in the same way, `Вакансия: <title>` and `Компания: <name>` (older mails say `компании:`), and links to `https://hh.ru/vacancy/<id>`.

| Subject | What Job Radar records |
|---|---|
| «Работодатель не готов пригласить вас (на собеседование)» | **Rejection** by the company, plus the applied vacancy and the application (below) |
| «Работодатель закрыл позицию» | The same: the company closed it without you |
| «Вам написали по вакансии…», «С вами хотят связаться из…», «Новое сообщение в чате» | The mail goes to the company's history as `recruiter_outreach` |
| «Вакансии по подписке: …» | A digest of new vacancies from your saved search: they go to the queue (below) |
| «Спасибо за ваше резюме!» | An employer's auto-reply without a company: left as it is |

**Applied vacancy and application.** hh.ru sends no mail when you apply, but a rejection means you did. So the first notice about a vacancy creates three things, all linked to the hh.ru page:
- the vacancy, in state `applied`, with source `hh` and `external_id` = the hh.ru vacancy id;
- an `Application` with source `hh`;
- the rejection, if the notice is one.

The application date is not in the mail, so it is set to the date of the first notice, and the application's note says so. Later notices about the same vacancy reuse what is already there.

**Already known from another source (#172).** Telegram channels and LinkedIn often carry the same vacancy. If exactly one vacancy of the company has the same title (ignoring case and spaces) and no hh.ru source yet, the notice attaches the hh.ru page to it instead of creating a second one; the vacancy becomes `applied`. Two hh.ru vacancies with the same title stay apart: they are different postings (another city or team). When several candidates match, nothing is merged.

Duplicates made before this fix are merged with `job-radar db merge-hh-duplicates` (a dry run that lists the pairs) and `--apply`. The older vacancy is kept; the hh.ru one gives it its sources, rejections, applications, notes, Telegram posts, analyses and chat, then is deleted. Back up `data/job_radar.db` first.

**The employer is sometimes a person.** Some vacancies list a recruiter or a sole trader as the employer, e.g. a full name. Job Radar keeps the name as it is: the mail has nothing better.

Without Gmail access these mails wait for the next run with access.

## Digests: «Вакансии по подписке» (#102)

hh.ru mails the new vacancies of your saved search ("автопоиск"). Since 2026-10-05 that search is **Python, any region, remote only, full-time**, so every vacancy in a digest is remote by construction.

**Only digests from the remote search are imported.** `JOB_RADAR_HH_DIGESTS_SINCE` (e.g. `2026-10-05`, in `.env`) sets the date of the change: older digests came from the earlier Moscow office search and are left alone. Without the setting no digest is imported.

For each vacancy in a digest:
- **Read from the mail:** title, salary, employer, city and the vacancy id. The digest body is read from Gmail and not stored, like other hh.ru mail.
- **Kept only when** the title names a developer-type role. This is the same title filter as for Telegram and Himalayas: engineer / developer / разработчик / architect / backend / …, and not analyst / manager / QA / teacher / author / …. The search already matched "Python" somewhere in the vacancy.
- **Location:** `Remote (<employer's city>)`. The employer's city is kept, because "remote" on hh.ru often still means "living in Russia": the score asks to check whether your home country is allowed.
- **Link:** source `hh` with `external_id` = the vacancy id, shared with the notices above, so a vacancy is never duplicated. It links to `https://hh.ru/vacancy/<id>`.
- **Rules:** the usual scoring and auto-ignore rules apply.
- **Description:** the digest has none, so the vacancy shows *verify manually*. Open it on hh.ru and click the **→ Job Radar** bookmarklet (below).

Each digest is imported once, then marked `hh_digest_done`; older digests stay `hh_digest`. The step runs within Mail analysis.

## Descriptions: the bookmarklet (#136)

The official API cannot be used: since 2025-12-15 dev.hh.ru registers apps only for employers (#101), and anonymous requests to `api.hh.ru` get 403. Job Radar does not fetch hh.ru pages by itself either; the one exception is the **Fill from Chrome** button, which opens the To review hh.ru vacancies without a description in your own Chrome and reads them with this same reader (#260, [ADR 0002](adr/0002-chrome-fill-hh.md), [linkedin-chrome-fill.md](linkedin-chrome-fill.md)).

Instead, open a vacancy on hh.ru (`hh.ru/vacancy/<id>`, any hh.ru subdomain) and click the same **→ Job Radar** bookmarklet as for LinkedIn ([linkedin-capture.md](linkedin-capture.md)).

**It reads from the page you opened:**

| Field | From |
|---|---|
| Vacancy id | the URL |
| Title, company | `data-qa="vacancy-title"`, `"vacancy-company-name"` |
| Salary | `data-qa^="vacancy-salary"` |
| Work format | `data-qa="work-formats-text"`, e.g. "Формат работы: удалённо или гибрид" |
| City | the first part of `data-qa="vacancy-view-raw-address"` |
| Description | `data-qa="vacancy-description"` |

**What Job Radar does with it:**
- **The vacancy:** the one with that hh.ru id (source `hh`) gets the description, or a new vacancy is created.
- **The location follows the work format:**
  - any "удалённо" → `Remote (<city>)`;
  - else "гибрид" → `Hybrid, <city>`;
  - else "на месте работодателя" → `On-site, <city>`.

  The hybrid / on-site auto-ignore rule then applies.
- **The checks:** the description is stored as a snapshot, so the Python, language, stack and AI checks run on it as for any vacancy.
