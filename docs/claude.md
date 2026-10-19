# Deep analysis with Claude

The local model (docs/ai.md) sorts every vacancy cheaply. For the few vacancies worth a closer look, Job Radar asks **Claude** through **Claude Code in print mode** (`claude -p`). It runs on your Claude subscription limits: no API key and no extra cost (issue #105).

## How to use it

1. **One vacancy:** press **✨ Claude** in its row. It is queued at once. The button is there only when the vacancy has a description: without one there is nothing to analyze, and the row says so (open the vacancy and click the bookmarklet). A row with a finished analysis has **↻ again** to run it once more (#146).
2. **Several:** tick them, or press **Select ≥ 6.5** to tick every row with that score or more and a description. Vacancies without a description are skipped when queued; the status says how many. On a phone, the tick is the big **Select** toggle in the row's buttons, away from the title link (#123). While anything is ticked, a bar stays at the bottom of the screen with the count and **Analyze with Claude**, so there is no scrolling back to the top. Press it. The vacancies are queued and analyzed in the background, **4 at a time** by default (about 20–60 s each; see *Parallel analyses* below). You can keep working, reload the page or close it.
3. Each finished vacancy updates its row by itself: a verdict badge (`apply` / `maybe` / `skip`), the match in %, and the analysis under **Claude**. The header shows the progress (`Claude: 2 queued, 1 running`). If you allow browser notifications, one pops up per finished vacancy.
4. You decide. Nothing changes the vacancy's state by itself; "ignore with reason" stays a click away.

## What Claude gets

| Part | From |
|---|---|
| Your CV | the file in `JOB_RADAR_CV_PATH` (e.g. `~/text/cv/yakovenko_python_en.md`) |
| Your profile | known / learnable / blockers / rules from [Profile](ai.md#stack-vs-your-cv) |
| Fixed facts | full remote only, you live in Thailand, no relocation, any timezone is fine, you speak Russian (native) and English |
| The vacancy | title, company, location, salary, the latest description |

Nothing from Gmail, company history or other vacancies is sent. The vacancy text and your CV go to Anthropic, as in any Claude conversation.

## What comes back

A fixed JSON structure (`--json-schema`):

| Field | Meaning |
|---|---|
| `verdict` | `apply`, `maybe` or `skip` |
| `match` | 0–100 |
| `summary` | Two or three sentences |
| `location` | `ok` / `risk` / `blocker` with a note and a quote |
| `strengths`, `gaps`, `red_flags` | Each item with a quote from the description; a gap says whether it is must-have or nice-to-have and whether it is a blocker for you |
| `questions` | What to ask the recruiter |
| `cv_keywords` | Words from the vacancy worth having in the CV |

Every quote is checked against the description, as for the local model. A quote that is not found is shown as *unverified*. The explanations are written in the language chosen in Settings (Russian by default); quotes stay as in the original.

## How it runs

- **Workers (#197):** the server runs a pool of analysis threads; each takes the oldest queued vacancy and runs `claude -p --output-format json --json-schema … --model <model> --tools "" --strict-mcp-config --setting-sources ""`, with the prompt on stdin.
  - No tools: Claude only reads and answers. It cannot open files, run commands or go online. The vacancy text is not ours, so an analysis never gets the internet: a line planted in a description could steer it (#213).
  - **The chat can search the web (#213)**, unless **Internet in chat** is off in Settings: its runs get `--tools WebSearch,WebFetch` (allowed without asking) and nothing else. LinkedIn and hh.ru are denied by Claude Code's own permission rules (`--disallowedTools "WebFetch(domain:linkedin.com)" "WebFetch(domain:*.linkedin.com)" …`, both forms: a bare domain does not cover `www.`), because the project never fetches them automatically. The risk that is left: a description could ask Claude to open a URL that carries something from the conversation; turn the switch off if that worries you.
  - `ANTHROPIC_API_KEY` is removed from its environment, so it never falls back to paid API billing.
  - **Parallel analyses** (Settings, 1–8, default 4) is how many run at once; a change applies without a restart. A vacancy is claimed atomically, so two threads never run the same one.
  - Chat questions have **their own thread**, outside that number: an answer never waits behind analyses.
  - The daily cap counts running analyses too, so parallel runs cannot pass it.
- **Sessions:**
  - each analysis is a Claude session (`--session-id`), kept so the vacancy chat (#106) can continue it with `--resume`;
  - sessions live under `data/claude/`, apart from your own Claude Code sessions.
- **Stored:** `claude_analyses` holds state, model, session id, the answer, the error if any, the notional cost Claude reports, and the prompt version. A server restart puts unfinished vacancies back in the queue.
- **Errors:**
  - **Transient errors are retried by themselves**, up to 3 attempts with 10 s and then 30 s in between, each with a fresh session. Transient means: Claude Code could not fit its answer into the schema (`error_max_structured_output_retries`, seen once in 27 runs; the same prompt worked on the next try), a timeout, or an overloaded / rate-limited / 5xx answer.
  - **The subscription's usage limit** ("usage limit", "rate limit", "429", "too many requests") is not retried at once: the vacancy goes back to the queue and **new analyses pause for 15 minutes**; the header says until when. Running ones finish as usual.
  - A missing `claude` or an expired login fails at once.
  - A failed row shows the reason, the number of attempts and a **retry** button (#113).

## Settings

| Where | Setting | Default |
|---|---|---|
| Settings page | Model | `sonnet` (`opus`, `haiku`) |
| Settings page | Analyses per day | 30; the rest wait in the queue until tomorrow |
| Settings page | Language of the explanations | Russian |
| Settings page | Internet in chat | on: the vacancy chat may use web search and open pages, except LinkedIn and hh.ru (#213) |
| Settings page | Parallel analyses | 4 (1–8): how many run at once; more finishes the queue sooner and uses the subscription's limit sooner (#197) |
| `.env` | `JOB_RADAR_CV_PATH` | none: without it, the analysis says there is no CV |
| `.env` | `JOB_RADAR_CLAUDE_COMMAND` | `claude` |
| `.env` | `JOB_RADAR_CLAUDE_WORKER` | `true`; set `false` on a test server over a copy of the database, so it never runs the queue a second time (#127) |

You need Claude Code installed and logged in on this machine (`claude` in a terminal, once).

## Vacancy page and chat

**Memory (#215).** The chat keeps facts about you and skills between conversations and every analysis reads them: [claude-memory.md](claude-memory.md).

Every vacancy has its own page, `/opportunities/<id>`: the **page →** link next to the title in the queue and in the Description window (#106). A row with a finished analysis also has **Chat with Claude →** (with the number of messages), which opens the page at the chat. It shows:
- title, company, location, salary and the source links;
- the description, with Python marked;
- the company history;
- the reasons behind the score (local checks and the local model);
- the Claude analysis, opened.

**Chat.** Below the analysis you can ask Claude anything about this vacancy, e.g. "how do I answer the TensorFlow requirement?" or "write a short cover note".
- Each question continues **the same Claude session** as the analysis (`claude -p --resume <session id>`), so Claude remembers the vacancy, your CV, your profile and everything said before. A question is answered in plain text in the language chosen in Settings.
- Questions have their own background thread, so they are answered while analyses run; they don't count towards the daily cap. The page shows *thinking…* and the answer when it is ready, without a reload.
- Every message is stored in `claude_messages` and shown on every visit. Each Claude answer has a **Copy** button that puts its plain text on the clipboard in one tap (#152).
- The chat needs a finished analysis: the page offers **Analyze with Claude** until there is one.
- **Quick prompts** (#144). Buttons above the chat box put a ready question into it; edit it if you like, then press **Ask Claude**. The buttons never send anything themselves.

  | Button | Asks for |
  |---|---|
  | Cover letter (EN) / Сопроводительное (RU) | A cover letter for this company and vacancy, 150–200 words, based only on your CV and the vacancy text (no invented facts or numbers), plain text ready to paste |
  | Why I fit | 3–5 bullets tying your CV to the vacancy's requirements |
  | Questions to ask | What to ask the recruiter about this job |
  | Reply to recruiter (EN) | A short, polite reply to a recruiter's message (paste their message after the prompt) |

  The prompts live in `job_radar/claude/prompt.py` (`QUICK_PROMPTS`).

Sending a message keeps the panel or the page where it is and the cursor in the text box: the redraws of the chat do not scroll (#188).
