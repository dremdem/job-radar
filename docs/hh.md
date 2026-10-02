# hh.ru (HeadHunter)

hh.ru sends its notifications from `noreply@hh.ru`. They reach Job Radar through the Gmail label `job-proposal` (see [gmail.md](gmail.md)). As with other private mail, their bodies are **not stored**: the Mail analysis step reads a body from Gmail, keeps what it found and drops the text (issue #99).

## Notifications

Each notice names the vacancy and the employer in the same way, `Вакансия: <title>` and `Компания: <name>` (older mails say `компании:`), and links to `https://hh.ru/vacancy/<id>`.

| Subject | What Job Radar records |
|---|---|
| «Работодатель не готов пригласить вас (на собеседование)» | **Rejection** by the company, plus the applied vacancy and the application (below) |
| «Работодатель закрыл позицию» | The same: the company closed it without you |
| «Вам написали по вакансии…», «С вами хотят связаться из…», «Новое сообщение в чате» | The mail goes to the company's history as `recruiter_outreach` |
| «Вакансии по подписке: …» | A digest of new vacancies (part 2, not yet) |
| «Спасибо за ваше резюме!» | An employer's auto-reply without a company: left as it is |

**Applied vacancy and application.** hh.ru sends no mail when you apply, but a rejection means you did. So the first notice about a vacancy creates three things, all linked to the hh.ru page:
- the vacancy, in state `applied`, with source `hh` and `external_id` = the hh.ru vacancy id;
- an `Application` with source `hh`;
- the rejection, if the notice is one.

The application date is not in the mail, so it is set to the date of the first notice, and the application's note says so. Later notices about the same vacancy reuse what is already there.

**The employer is sometimes a person.** Some vacancies list a recruiter or a sole trader as the employer, e.g. a full name. Job Radar keeps the name as it is: the mail has nothing better.

Without Gmail access these mails wait for the next run with access.
