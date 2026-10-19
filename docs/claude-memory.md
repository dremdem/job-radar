# Claude memory (#215)

Claude keeps notes between conversations, in the spirit of Hermes Agent's self-improving skills. What you explain once in one vacancy's chat (where you live, what salary you name, how to answer a recruiter's usual question) it remembers in every later chat and analysis.

## What it keeps

Two kinds of entries, each with a short name, a one-line description and a body:

- **fact**: something about you that is not in the CV or the profile, e.g. "lives in Thailand; needs remote without an EU-country residence", "salary: from 4000 USD net a month, no figures before the call".
- **skill**: how to do a recurring thing well, e.g. "how to check whether a Polish B2B contract allows working from Thailand", "how to answer the salary question", "company check list". Claude improves a skill when a conversation shows a better way.

## How it is written

- Only in the **vacancy chat**, by Claude itself, with no confirmation: you decided it is on its conscience. Analyses read the memory but never write it.
- At the end of an answer Claude may add a block that Job Radar strips from the text and applies:

  ```
  <memory>
  [{"op": "save", "kind": "fact", "name": "salary", "description": "what salary to name", "body": "..."},
   {"op": "delete", "name": "old-entry"}]
  </memory>
  ```

  `save` adds an entry or replaces the one with the same name. A block that cannot be read is ignored, and the answer says so.
- Under that answer the chat says what changed, e.g. "Memory: saved salary, deleted old-entry", with a link to the Memory page.
- Claude is told to save only what you said or confirmed, or a way of working that proved useful, and **never because the vacancy text or a web page asks for it** (they may carry planted instructions). The change log below is how you catch it if it does.

## How it is read

Every chat answer and every analysis gets the whole memory in its prompt: facts first, then skills, each as name, description and body. The memory is small by design: an entry's body is cut at 2000 characters, and past 60 entries or 15000 characters in all Claude is told the memory is full and to merge or delete entries before adding more.

## The Memory page

**Memory** in the header (`/memory`):

- every entry, facts first, with when it changed and the vacancy whose chat changed it;
- **edit** and **delete** for each, and a form to add one yourself;
- the **change log**: the last 100 changes, who made them (Claude or you), and the old and the new text, so a bad change can be undone by hand.

Stored in the database: `claude_memory` (the entries) and `claude_memory_changes` (the log).
