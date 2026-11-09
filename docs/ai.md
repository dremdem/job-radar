# Description analysis (local model)

Job Radar asks a **local** model to read vacancy descriptions for what the title and location fields don't show (issue #93):
- whether the job is hybrid or on-site;
- whether the candidate must live in specific countries.

It runs through [Ollama](https://ollama.com) on this machine, so there is no paid API and no description leaves the computer. The default model is `gemma3:4b`: 3–9 s per description on an 8 GB GPU.

## Off by default (#223)

Since the [Claude analysis](claude.md) the step is **off** unless `JOB_RADAR_AI_ANALYSIS=true`. Measured on 2026-10-23:

- it filtered nothing before Claude: the auto-ignore rule below was off, so 0 vacancies were ignored by it;
- of the 64 vacancies it flagged that Claude also analyzed, Claude said *skip* for 46 and *maybe* for 17, never *apply*; Claude reads work mode, residence and stack gaps itself, with reasons;
- it costs memory while a model is loaded and 8.6 GB of disk for the models.

What is lost while it is off: the `AI:` and `Requires …` reasons and their score penalties on new vacancies, the stack check against your Profile lists, and the "AI: hybrid / country-only" auto-ignore rule (it finds nothing). Vacancies analyzed earlier keep their reasons. Turn it back on with the variable and a running Ollama.

## Rule: the model extracts, code decides

The model only answers a fixed set of questions and must copy the **exact sentence** that supports each answer:

| Fact | Values |
|---|---|
| `work_mode` | `remote`, `hybrid`, `onsite`, `unknown` |
| `residence` | Countries / regions / cities where the candidate must live or be allowed to work. Empty = anywhere, or not stated |

Code then checks every quote against the description (case and spacing ignored). **A fact whose quote is not in the text is dropped**: small models sometimes invent a sentence, and Job Radar must not invent facts. What remains becomes a reason with the quote:

| Finding | Reason in the queue | Score |
|---|---|---|
| Hybrid / on-site | `AI: hybrid — “<quote>”` | −3 |
| Residence that is not worldwide / anywhere / your `home` / one of your `home_regions` ([Profile](#stack-vs-your-cv)) | `AI: only for <places> — “<quote>”` | −2.5 |

Timezones are not a restriction: the candidate can work in any timezone.

## When it runs

- Last step of **Sync** / `./jr sync`, and alone: `uv run job-radar analyze`.
- It covers vacancies in **To review** whose latest description has not been analyzed yet, at most 150 per run, newest first. Each description is analyzed once (`description_analyses`, one row per description snapshot).
- If Ollama is not running or the model is missing, the step reports it and the rest of the sync goes on.

The optional auto-ignore rule **"AI: hybrid / on-site or country-only"** in [Settings](settings.md) moves such vacancies out of To review. It is off by default.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `JOB_RADAR_AI_ANALYSIS` | `false` | Run the step in Sync |
| `JOB_RADAR_OLLAMA_URL` | `http://localhost:11434` | Ollama server |
| `JOB_RADAR_AI_MODEL` | `gemma3:4b` | Model; install with `ollama pull gemma3:4b` |
| `JOB_RADAR_AI_MAX_PER_SYNC` | `150` | Descriptions analyzed per run |

## Measured quality (2026-10-01, `gemma3:4b`, RTX 4060 Ti 8 GB)

- **Labelled vacancies** (prompt tuning, #93):
  - Of 25 vacancies ignored with an "only <country>" reason, 17 were found. Two of the misses state the restriction only in LinkedIn's location field, not in the description; that field is scored already.
  - Of 17 applied vacancies, 7 were flagged. 4 of those are real restrictions (UK, Vietnam, EU/UK, Europe). 3 are wrong: an optional office or team travel read as hybrid.
- **To review:** 79 descriptions took 1 min 53 s, and 11 were flagged. All 11 are correct on reading (Cyprus office, Yerevan, Warsaw/Limassol hybrid, "must hold EU citizenship", Latin America).
- **Rejected by code:** these model answers are rejected by code, not by the prompt:
  - exclusions ("except Russia and Belarus");
  - preferences ("preference for candidates in Poland");
  - flags of a language level (🇬🇧 English B2+);
  - "any city".
- **Rejected prompts:**
  - example places in the prompt: the model copied them into its answers;
  - asking for facts without quotes: more hits, but twice the false alarms.
- **`qwen3:8b`:** no better, 3× slower, and it loops under a strict JSON schema.

Quality depends on the prompt, so treat the reasons as **marks to check, with the quote**, not as verdicts. That is why the auto-ignore rule is off by default.

## Stack vs your CV

Vacancies that **require** a technology you have never worked with get marked and lowered, not ignored (issue #95).

1. **Your profile.** It lives in `data/profile.yaml` (git-ignored), and you can edit it on the **Profile** page (`/profile`, linked from the header) or by hand; the page reads and writes the same file (#104). It has your home and four lists:

   | List | Meaning | A required one costs |
   |---|---|---|
   | `known` | Technologies from your CV | nothing |
   | `learnable` | Not a blocker, e.g. something you tried or can pick up fast | −0.5 |
   | `blockers` | A separate field you won't learn in days, e.g. professional TensorFlow | −3, shown as a blocker |
   | `rules` | Free-text rules in your words ("No relocation.", "I speak Russian (native) and English (B2+)") | passed to Claude (#105); code does not interpret them |

   | Home field | Meaning | Used by |
   |---|---|---|
   | `home` | The country you live in and work from, e.g. `Thailand` | the score (`<home> listed` +1, "check if <home> is allowed"), the AI residence check (not a restriction), Claude ("I live in <home> and will not relocate") |
   | `home_regions` | Regions that include it, e.g. `Asia`, `APAC` | the score and the AI residence check, like `home` |

   Both are optional. Without them nothing is treated as your place: the score says "check if your country is allowed". A new `home` changes the score of vacancies imported after it; the AI reasons follow it at once.

   A technology in none of the lists costs −1. Without the file the stack check is skipped. Copy `profile.example.yaml` to start.
2. **Unknown technologies.** Job Radar has a catalogue of technologies common in backend vacancies: languages, data stores, queues, cloud, infrastructure, observability, data / ML tools, front-end frameworks. Each one has a match pattern; for example, `Go` matches only as a capitalised word or `Golang`. Everything in the catalogue that is not in your profile counts as unknown. Basics such as Git, SQL, REST and Linux are not in the catalogue.
3. **The question.** For every unknown technology the description mentions (at most 8), the local model answers one question: is it **required**, **nice to have**, or **not needed** (only the company's stack or another team)? It also gives the sentence that shows it. This takes about 1 s per question.
4. **What counts as a gap.** Code keeps a gap only when all of these hold:
   - the answer is "required";
   - the quote is in the description and names the technology;
   - the quote does not offer an alternative you know. Choices are spotted by "or", "one of", "at least one", "any", "или", "не принципиален", or a slash next to a known name: "strong in at least one of Kotlin, Go **or TypeScript**" and "AWS/GCP/Azure" are not gaps when TypeScript or AWS is in your profile, while "CI/CD" is no choice at all;
   - the quote does not call the technology a plus ("a plus", "bonus", "preferred", "будет плюсом", "желательно"). Then it counts as nice to have.
5. **In the queue.** `Requires Go (not in your CV) — “<quote>”` (−1), `Requires RabbitMQ (learnable)` (−0.5), `Requires TensorFlow (blocker)` (−3). Non-blockers cost −3 at most together; blockers are not capped. Levels are applied when the queue is shown, so moving a technology between lists changes the score at once. A technology added to `learnable` or `blockers` that the description mentions is asked about at the next Sync. There is no auto-ignore rule for this: you decide.

**Measured** on 79 vacancies in To review: 2 min 37 s; 31 have at least one gap (most often Spark, PyTorch, ClickHouse, Redis, Databricks). The rules for alternatives and optional wording were added after reading every "required" answer.

The result is stored in `description_analyses.stack`. A description analyzed before the profile existed is checked on the next run.

| Variable | Default | Meaning |
|---|---|---|
| `JOB_RADAR_PROFILE` | `data/profile.yaml` | Your profile |
