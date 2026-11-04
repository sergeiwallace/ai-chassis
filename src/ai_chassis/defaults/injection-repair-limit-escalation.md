---
title: "Injected prompt — /start-goal repair and retry limit exceeded, escalate to the maintainer"
category: injection-template
status: active
source: ISSUE-REF
design: docs/designs/issue-ref-prompt-injection-library.md
fields: [delegation_target, run_id, signature, signature_human, rounds, max_rounds, round_issues, run_record_path, attempt_paths, target_session]
rendering: "Python str.format over the body below the closing front-matter rule; every field is required; every rendered line stays under multiline_mode.chunk_chars"
---
Escalation: /start-goal repair and retry limit exceeded in this session ({target_session}): {rounds} rounds of a /goal injection followed by a /delegate {delegation_target} /bug-fix repair have not produced a /goal that passes both pre-submission and post-submission validation (run {run_id}, failure signature {signature_human} = {signature}, configured limit {max_rounds}; repair issues so far: {round_issues}). The maintainer must review this: the /start-goal prompt-injection mechanism may need a rethink.
Do NOT retry the /goal injection and do NOT launch another /bug-fix for this signature. Instead: (1) run /beads-task to create a Beads issue titled "/start-goal repair and retry limit exceeded, needs the maintainer's review ({signature_human})" whose description carries this message, the run record path {run_record_path} and the attempt records {attempt_paths}, linked to the per-round issues {round_issues}; set it in_progress. (2) Create the matching CC task with TaskCreate and set it in_progress with an owner via TaskUpdate, so it shows on the task panel, which is the surface the maintainer reads. (3) Write the held item for the maintainer with the full round history (each round's failure, its issue, its PR, its mark-fixed result) and the observability pointers above. (4) Surface it in /exit-goal style: a GOAL TERMINAL line naming this item, what it waits on (the maintainer's review of the /start-goal prompt-injection mechanism) and what was tried (the {rounds} rounds and their fixes).
Then stop. The self-heal loop stays disarmed for this signature until the maintainer clears it with: python -m ai_chassis.prompt_injection self-heal clear --signature {signature}.
