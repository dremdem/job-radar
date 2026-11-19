Responses of the public job board APIs, written by hand in the shapes their official
documentation gives (the cloud where this was built cannot reach the vendors), trimmed to
the fields Job Radar reads. Companies, jobs and ids are made up.

- greenhouse_jobs.json: `GET boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true`
  (https://developers.greenhouse.io/job-board.html); `content` is escaped HTML.
- greenhouse_board.json: `GET boards-api.greenhouse.io/v1/boards/<slug>`.
- lever.json: `GET api.lever.co/v0/postings/<slug>?mode=json` (https://github.com/lever/postings-api).
- ashby.json: `GET api.ashbyhq.com/posting-api/job-board/<slug>`
  (https://developers.ashbyhq.com/docs/public-job-posting-api).
- workable.json: `GET apply.workable.com/api/v1/widget/accounts/<slug>?details=true`.
