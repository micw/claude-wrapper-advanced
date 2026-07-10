# Assumption integration tests

The proxy is built on ~30 empirically discovered behaviours of the Claude Code CLI and the
Anthropic backend. A CLI update can silently break any of them. `assumptions.py` checks them in
isolation and prints a checklist: **what still works, and what you need to look at.**

The tests use the real `app._build_args` — so they exercise the CLI **and** our wrapper together.
Results stream out one by one (each line appears as soon as its check finishes).

## Workflow on a new CLI version

```bash
# 1) Fast & free: instantly catches renamed/removed flags and auth-shape changes.
python tests/assumptions.py --offline

# 2) Full: verifies the actual behaviour (needs login, costs a few tokens/minutes).
python tests/assumptions.py
```

- **Tier 1 (OFFLINE):** CLI version, presence of every flag we rely on in `--help`,
  `auth status --json` shape, `setup-token`. ~0 tokens, seconds. Run this first.
- **Tier 2 (ONLINE):** stream-json protocol, token-free `/clear`, "replies to every msg", reuse,
  interrupt, usage shape (drift), native `tool_use`, caching (ttl required, identical, incremental),
  model aliases.
- **MANUAL:** opus 1M context (needs a >200k prompt) and system-prompt sections (via capture proxy)
  — listed as lines in the output, not automated.

Exit code != 0 if any assumption **FAILs**. Use `--json` for machine-readable output (CI).

## What a FAIL means

The assumption is broken -> **review the affected code before rolling out the new CLI version.**
The `->` line shows the observed value. Example: if `cache.ttl_required` fails, check whether
`translate.py` still sets the `cache_control` format correctly.

## Adding an assumption

```python
@check("area.name", tier=2, "short description of the assumption")
async def c_xyz(ctx):
    async with CLI(mcp_tools=[...], model="sonnet") as cli:
        ev, _ = await cli.turn("...")
        return OK("observed…") if condition else FAIL("what differed")
```

`ctx` is a shared dict (e.g. `tools.native_tooluse` passes its observation to
`tools.no_result_on_tooluse`). Use `SKIP(...)` for non-applicable cases.
