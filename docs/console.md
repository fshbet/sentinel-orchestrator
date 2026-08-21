# The web console

A single self-contained page at `ui/console.html`, built on the Viyana UI/UX
v3.1 "Violet Ember" system. The orchestrator serves it at `/` when the API is
running, so it talks to `/v1` on the same origin — no CORS, no config.

```bash
orchestrator serve
```

Then open <http://localhost:8080/>.

Opening the file directly also works. It probes for an orchestrator on
`localhost:8080` and, when none answers, falls back to worked sample data and
says so in the header. It never renders sample data as though it were live.

## What it shows

| Panel | What it answers |
| --- | --- |
| **Verdict** | Did it finish, and how much should you trust it? Status and confidence are shown together, because "completed (uncertain)" is not the same result as "completed (confirmed)". |
| **Devil's advocate** | What is the case against this result? |
| **Plan** | What did it actually do, in what order, with which agent and model. |
| **Checks** | Which validators ran, what they concluded, and which were advisory. |
| **Decision** | An approval or an input request, when the run is waiting on you. |
| **Decision trail** | The full audit sequence — every event, in order. |

`Cmd/Ctrl+K` opens a command palette that searches runs and commands.

## The devil's advocate panel

The advocate is a real validator (`src/orchestrator/validation/advocate.py`),
not a UI flourish. Every other check asks whether the work passed; this one
asks what would make it wrong. It runs as a separate model call from the one
that produced the work, because asking a conversation to critique itself gets
agreement rather than scrutiny.

Enable it on a task by adding it to that task's validation list:

```json
{"validator": "devils_advocate"}
```

It is advisory by default: it lowers confidence and surfaces objections, but a
suspicion is not a defect, so it does not fail a task on its own. To make a
critical objection stop a run:

```json
{"validator": "devils_advocate", "mandatory": true, "config": {"block_on": "critical"}}
```

Three rules keep it useful rather than merely negative:

* **Every objection names what would settle it.** An objection with no
  resolution is a complaint; the parser drops those before they reach you.
* **It reaches `likely` at best**, never `confirmed`. An argument is an
  inference however well made.
* **The panel distinguishes three states**, and the first two are opposite
  claims about how much scrutiny happened:
  * it did not run — nothing has been argued against,
  * it ran and found nothing substantive,
  * it ran and raised objections, ranked critical / substantive / minor.

When a run is waiting on your approval, the objections are rendered **above**
the Approve button. Reading the case against the work after clicking Approve is
reading it too late.

## Design notes

Violet is the primary; coral marks the advocate. That is deliberate: coral is
the brand's human-insight and editorial marker, and an argument about the work
must not be mistaken for a red failure state. The advocate's own severity
stripes do use the error and warning tokens, but only inside an objection,
where they rank the argument rather than report a system fault.

Glass is used only on the command palette. Light and dark are both complete
themes and the toggle wins over the OS setting in either direction.

## Authentication

The console is served by the orchestrator itself, so it uses whatever
authentication that instance requires.

* **Loopback, no token** — the default single-user setup. Nothing to do.
* **Token required** — the console shows a sign-in card. The token is kept in
  `sessionStorage` for that tab only, never written to disk, and cleared if the
  server rejects it.

When a token is required the console shows nothing until it has one. It does
not fall back to sample data, because worked examples sitting beside a sign-in
prompt read as though they were that instance's real runs.

See [SECURITY.md](../SECURITY.md) for how to configure and rotate tokens.
