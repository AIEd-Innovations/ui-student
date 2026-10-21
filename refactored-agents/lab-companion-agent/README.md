# Lab Companion agent

A standalone version of the Student UI's Lab Companion, organized like the old
single-agent example. It takes a question and supplied context and returns a
**draft hint**. The original Lab Companion prompt text is unchanged. It does
not run the Student UI, Guardian, or course-material retrieval.

## What changed

- `base_agent.py` and `lab_companion.py` separate the base agent from the hint logic.
- `llm.py` replaces Azure and direct OpenAI calls with Vercel AI Gateway. The
  default model is `google/gemini-2.5-flash`.
- `test.py` checks the agent locally; `dev_server.py` and `index.html` provide a
  small browser form; `api/index.py` serves the same `POST /api` route on Vercel.

## Run locally

Use Python 3 and npm. There are no packages to install. In this folder, set
your Gateway key for the current terminal session, then run:

```zsh
read -r -s "AI_GATEWAY_API_KEY?Paste Gateway key: "
echo
export AI_GATEWAY_API_KEY
python3 test.py
python3 test.py --live
npm run dev
```

Open <http://localhost:3000>, enter a question and optional context, and click
**Get draft hint**. Stop the server with Ctrl+C. The first test command does not
call Gateway; `--live` and the browser form do.

## Vercel preview

The preview serves the same form and accepts `POST /api` with JSON such as
`{"question":"How do I start?","context":"Identify the terminals."}`. A
successful reply is `{"status":"completed","response":"..."}`. Set
`AI_GATEWAY_API_KEY` in the Vercel project's **Preview** environment, then run
`npx vercel deploy` from this folder to publish updates. Keep preview access
protected: the endpoint has no separate authentication and Gateway calls use
credits. Local `.env*` files are excluded from deployment and Git.
