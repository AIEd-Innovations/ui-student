# Guardian agent

A standalone preview of the Student UI Guardian, organized like the Lab
Companion refactor. It classifies student questions, sets a guidance level,
counts violations, and reviews draft hints. The classification and verification
prompts and guidance thresholds come from `ui-student/agentic_system/policy/engine.py`.
It uses Vercel AI Gateway through a small Python REST adapter; the default
model is `google/gemini-2.5-flash`.
Classification and review requests ask Gateway for schema-constrained JSON.

## Run locally

Use Python 3.12 and npm. No packages need installing. Set your existing Gateway
key in the terminal where you start the server:

```zsh
read -r -s "AI_GATEWAY_API_KEY?Paste Gateway key: "
echo
export AI_GATEWAY_API_KEY
python3 test.py
npm run dev
```

Open <http://localhost:3000>. The form uses `POST /api` for both actions.
`python3 test.py` uses fake model replies and does not call Gateway. A live
check is available with `python3 test.py --live` after setting the key.

## API

Classify a question:

```json
{"action":"validate","question":"What does voltage mean?","lab_id":"lab-1","context":"","conversation_history":[],"session_state":{"question_count":0,"violation_count":0,"escalated":false}}
```

The reply contains `classification`, `guidance_level`, violation details, and
the updated `session_state`. Pass that state into the next validation request.
If the model cannot classify the question, the API returns an error instead of
a guessed classification. `classifier_fallback` is `false` on a real decision.

Review a draft:

```json
{"action":"verify","question":"What does voltage mean?","draft_response":"Voltage is electric potential difference.","guidance_level":"FULL","classification":"CONCEPTUAL"}
```

The reply contains `passes`, `reason`, and any `feedback_for_companion`.
If the model cannot review the draft, the API returns an error instead of
marking it as passed. `verifier_fallback` is `false` on a real decision.

## Vercel preview and integration

`api/index.py` is a Vercel Python Function. The root `index.html` is the same
test page shown locally. Put `AI_GATEWAY_API_KEY` in the Vercel project's Preview
environment before deploying this folder. Protect preview access: the API has
no authentication and model calls use credits. Local `.env*` files are ignored.

This preview intentionally has no database or course retrieval. The browser
keeps its own counters, which a user can reset or alter. For real student use,
the caller must load and save session state in trusted durable storage, bind it
to an authenticated student/session, and supply authoritative conversation
history and course excerpts. Do not use browser-provided counters to enforce
academic-integrity policy. The original Guardian's session records, violation
logs, and end-of-session reports remain in `ui-student`; they have not been
migrated here.

The original Student UI Guardian treated classifier errors as `PROCEDURAL`
and verifier errors as passing. This standalone preview reports those errors
explicitly so a failed Gateway call cannot appear to approve a draft.
