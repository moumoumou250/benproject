from __future__ import annotations
from typing import Any
from agents import function_tool

@function_tool(strict_mode=False)
async def get_threat_model(target: str) -> str:
    """Read this scan's threat model for a target, if an agent has derived one.

The threat model is this run's shared answer to who the attacker
is, where the trust boundaries sit, and what counts as critical
here. Call it before you start hunting so you inherit the shared
view instead of re-deriving it, and so every agent on this run
agrees on what "attacker-controlled" means.

It is scoped to this scan and nothing is carried over from an
earlier run, so an empty result means no agent has derived one yet.

Works black-box or white-box. The target can be a host, a URL, an
API base, or a repository path; equivalent spellings of the same
host resolve to the same model, and a checkout resolves to its
remote, so a model derived white-box by one agent is read back by
another testing the deployment.

Returns ``found: false`` when nothing has been derived yet — derive
one and share it with ``save_threat_model``.

Any ``amendments`` in the response are corrections other agents
recorded after the base model was written. They are part of the
model — read them, and prefer the later statement where one
contradicts the base text.

Args:
    target: What the model describes — a host or URL
        (``https://app.example.com``), or a repository path
        (``/workspace/myrepo``). Use the same value the scan was
        pointed at, so agents converge on one model."""
    return ''

@function_tool(strict_mode=False)
async def save_threat_model(target: str, content: str) -> str:
    """Share a target-scoped threat model with the other agents on this scan.

The model lives for this run only — it is not written to disk and a
later scan of the same host or tree starts without it.

**This replaces the whole document, and clears any amendments** —
it is for the agent establishing the baseline (normally root,
before subagents start), or for folding accumulated amendments back
into the body. If a model already exists and you only need to
correct or extend part of it, call ``amend_threat_model`` instead;
saving over it will silently discard whatever other agents added.

**Write it from whatever evidence you have.** With source, ground
it in the code and name the files, entrypoints, and controls that
make each claim true. Black-box, ground it in recon: the hosts and
ports that answered, the technology fingerprints, the observed
roles and tenants, the authentication and session model, the
endpoints and parameters you enumerated. A black-box model is
necessarily provisional — say which parts are inferred rather than
observed, and let later agents amend it as the picture fills in.

**Scope it to the target, not to your slice of it.** Do not centre
it on the diff you were handed, the subsystem you were assigned, or
the one host that happened to answer first. With source, distinguish
real product and runtime surfaces from test, docs, example, and
developer-tooling paths — in a monorepo, do not let ``tests/`` or
one-off scripts become the centre of gravity unless the code shows
they are genuinely deployed. Where the target documents its own
boundary — an ``AGENTS`` file, a specific ``SECURITY.md``, a
published API spec, an engagement scope — build on it rather than
inventing a competing story.

Structure the content in Markdown with these sections:

- **Overview** — what the target actually is, its real-world usage,
  and which parts are product/runtime versus tooling or
  non-production.
- **Trust Boundaries and Assumptions** — the boundaries, the actors
  on either side, and the invariants that must hold. Separate
  attacker-controlled, operator-controlled, and
  developer-controlled inputs explicitly. Black-box, this is the
  role, tenant, and privilege model: who can reach what before
  authenticating, as a low-privilege user, and across tenants.
- **Attack Surface and Attacker Stories** — the exposed surfaces
  (hosts, endpoints, parameters, integrations, or the code-level
  entrypoints and sinks), the mitigations already present that
  materially change severity or reach, realistic attacker stories,
  and the stories that are *not* realistic here and why.
- **Severity Calibration** — what critical / high / medium / low
  look like for *this* target, with a concrete example at each
  level. Where a vulnerability class needs attacker control that
  does not exist in real usage, say so here.

Args:
    target: What the model describes — a host or URL
        (``https://app.example.com``), or a repository path
        (``/workspace/myrepo``). Use the same value the scan was
        pointed at.
    content: The full threat model in Markdown."""
    return ''

@function_tool(strict_mode=False)
async def amend_threat_model(target: str, addendum: str) -> str:
    """Correct or extend the existing threat model without replacing it.

The baseline is written before anyone starts hunting, so it is
written with the least information anyone will ever have. That is
doubly true black-box, where the model starts as inference over
recon output and only becomes real as agents authenticate, map
roles, and reach the surfaces behind them. When your work
contradicts the model or fills in something it missed, record that
here — every agent that calls ``get_threat_model`` afterwards sees
your addendum next to the base model.

Amendments are append-only and attributed, so two agents amending
at once both survive. That is the difference from
``save_threat_model``, which overwrites the document and drops
every amendment on it.

Worth amending:

- A boundary the model calls trusted that you found is
  attacker-reachable, or vice versa.
- A host, endpoint, parameter, role, sink, or shared control the
  model does not mention.
- Something the model only inferred that you have now observed — or
  that turned out not to be true.
- A severity call the model got wrong for this target, with the
  reason.
- An assumption you disproved — the model says input is validated
  upstream and you found the path that skips it.

Not worth amending: individual findings (those are reports), or
restating what the model already says.

Args:
    target: What the model describes — the same host, URL, or
        repository path used to save it.
    addendum: The correction, in Markdown. State what the base
        model says, what is actually true, and the endpoint, host,
        file, or control that proves it."""
    return ''

