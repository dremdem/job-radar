# Description analysis (local model)

Job Radar asks a **local** model to read vacancy descriptions for what the title and location fields don't show (issue #93):
- whether the job is hybrid or on-site;
- whether the candidate must live in specific countries.

It runs through [Ollama](https://ollama.com) on this machine, so there is no paid API and no description leaves the computer. The default model is `gemma3:4b`: 3–9 s per description on an 8 GB GPU.

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
| Residence that is not worldwide / anywhere / Thailand / Asia / APAC | `AI: only for <places> — “<quote>”` | −2.5 |

Timezones are not a restriction: the candidate can work in any timezone.

## When it runs

- Last step of **Sync mail** / `./jr sync`, and alone: `uv run job-radar analyze`.
- It covers vacancies in **To review** whose latest description has not been analyzed yet, at most 150 per run, newest first. Each description is analyzed once (`description_analyses`, one row per description snapshot).
- If Ollama is not running or the model is missing, the step reports it and the rest of the sync goes on.

The optional auto-ignore rule **"AI: hybrid / on-site or country-only"** in [Settings](settings.md) moves such vacancies out of To review. It is off by default.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `JOB_RADAR_AI_ANALYSIS` | `true` | Run the step in Sync mail |
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
