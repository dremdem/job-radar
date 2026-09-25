# Review queue

The main page (`/`) lists opportunities in tabs:

| Tab | Shows | URL |
|---|---|---|
| **To review** (default) | `new`: not processed yet | `/` or `/?tab=review` |
| **Interested** | `interested`: the ones you plan to apply to | `/?tab=interested` |
| **Applied** | `applied` | `/?tab=applied` |
| **Ignored** | `ignore` | `/?tab=ignored` |
| **All** | everything | `/?tab=all` |

Each tab label carries its count. Inside a tab the order is: score (after penalties), highest first; vacancies of companies that rejected you at the bottom.

## Buttons

- `interested` / `ignore` / `applied`, *ignore with reason* and `rejected` act at once. In a filtered tab a vacancy that no longer belongs there **disappears immediately** and the counts update, no reload needed. In **All** the row stays in place and only its state changes.
- There is no "back to new": find the vacancy in **Ignored** or **All** and pick another state.

Company history, reasons and notes: [applications.md](applications.md).
