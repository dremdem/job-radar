# Skills gap report (`/skills`)

Which technologies you lack are asked for most, and which ones, once learned, would open the most vacancies (#154). Link **Skills** in the header.

## Where the gaps come from

For every vacancy in the chosen states and period, Job Radar collects the technologies it **requires** that are not `known` in your [profile](ai.md#stack-vs-your-cv):

- **Claude's analysis** ([claude.md](claude.md)): each gap with `must_have` or `nice_to_have`;
- **the local stack check** ([ai.md](ai.md#stack-vs-your-cv)): technologies the local model found *required*.

**Names are normalised** through the stack catalogue: "PySpark" and "Apache Spark" become *Spark*, "K8s" becomes *Kubernetes*. Gaps that are not technologies go to a separate list:
- degree;
- years of experience;
- leadership / management;
- language;
- domain.

## The table

| Column | Meaning |
|---|---|
| Must-have | Vacancies that require it |
| Nice-to-have | Vacancies that list it as a plus |
| **Would unlock** | Vacancies where it is the **only** missing must-have and nothing else blocks: no language requirement, no residence restriction or hybrid / on-site found by the checks, Claude's location verdict is not a blocker, and no non-tech blocker |
| Avg score | Average score of the vacancies that require it |
| In profile | known / learnable / blocker / not listed |

Rows are sorted by *Would unlock*, then by *Must-have*. Click a column header to sort by it, and click again to flip the order (#156). Open a row for the vacancies with their quotes.

**Best set.** Above the table, Job Radar picks the 1, 2 and 3 technologies that together unlock the most vacancies, using a greedy pick. A vacancy is unlocked when all of its missing must-haves are in the set and nothing else blocks it.

**Filters:**
- period: 30 days, 90 days or all;
- **Count:** *vacancies still open* (the default, explained below) or *all analysed*, which shows market demand across every vacancy, including ones you ignored for location or language;
- vacancies still open means To review, Interested and vacancies you ignored for a *stack* reason. Vacancies ignored for location, language or anything else are left out: learning won't open them.

**Actions** in each row set the technology's level in your profile: **known**, **learnable** or **blocker**. Moving one to *known* removes it from the gaps everywhere at once.
