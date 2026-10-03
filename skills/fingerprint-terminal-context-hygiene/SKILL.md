---
name: fingerprint-terminal-context-hygiene
description: "Audit and tidy Claude Code memories, history and temporary context for isolated Fingerprint Terminal profiles, with scoped edits and cleanup by age."
---

# Fingerprint Terminal Context Hygiene

Keep the isolated terminal's remembered context useful and current. Prefer short, factual English summaries and preserve the project details needed for normal work.

## Discover the actual scope

Run this skill on the host, where the profile configuration and storage are visible. Read `$FT_PROFILES_FILE` when set, otherwise `~/.config/fingerprint-terminal/profiles.json`. Do not initialize a missing configuration during an audit. Select the requested profile, normally `strict-auto-ip`, and verify `sandbox.mode=strict` and `home.mode=isolated` before editing its private context.

Profile storage is normally `~/.local/share/fingerprint-terminal/profiles/<profile-id>/`; the isolated HOME is its `home/` directory. Read `.claude/settings.json` there for `autoMemoryDirectory`: expand `~` against the isolated HOME, never the host HOME. Inspect `.claude/CLAUDE.md`, rules, active memory files and the memory index. Check project memory directories as well; a custom global directory does not establish that old project memories are irrelevant.

Use only the active profile's explicit `sandbox.shares`. An empty list is valid. Never assume `~/code`, add a share, or scan the host HOME. Respect `sandbox.hidden_paths` and symlink boundaries. Shared instructions may affect context, but ordinary shared work files are not disposable context.

The bundled helper is read-only and produces counts, paths and matching line numbers without printing file contents:

```bash
python3 scripts/audit_context.py --profile strict-auto-ip
```

Paths are relative to this skill directory. The helper reports skipped files and truncation; inspect specific relevant files before drawing semantic conclusions. It does not verify the live network exit, decide whether a process is idle, or prove that a pattern match describes the user.

## Audit and propose changes

During an `arch-rollup-maintainer` system update session, this skill is a required maintenance step. If that skill is installed, follow its `references/fingerprint-terminal-maintenance.md` for the context audit, checkout/dependency checks and refresh of existing private runtimes after a successful host update. Report each step's result. Update-session invocation alone does not authorize deleting conversation history or creating global Claude instructions.

For a request to inspect, list or dry-run, read only. Do not run `fingerprint-terminal conversations cleanup`, open the graphical conversation manager, instantiate `ConversationManager`, launch a terminal, or start a timer: those paths can create state or purge expired trash.

Classify findings by meaning:

- Correct outdated or contradictory notes using facts the user supplied. Preserve useful business and technical details.
- Summarize narrative and user quotations in English. Preserve exact filenames, identifiers, shell commands, product names, node labels and UI text when needed to operate the project.
- Keep server regions, client locations, accounting currencies and study material attached to their projects. Record personal work background only when the user supplied it and it is relevant to the requested task.
- Distinguish a past mistaken inference from a current assertion. Prefer a short durable correction over a repeated narrative of the old mistake.
- Treat stale paste caches, logs, plans, file snapshots and conversation artifacts as retention candidates. Establish their use, age and recovery value before deletion; do not delete a whole conversation for one regional keyword.

Report concrete paths, evidence, proposed action and any recovery cost. The observed installed conversation cleanup timer runs daily and purges conversation-manager trash after 14 days; verify the current units and implementation before relying on that behavior. It does not imply that memories or paste caches are cleaned.

## Apply the requested maintenance

Interpret a request to edit memories as permission to make the scoped edits. It does not automatically authorize bulk deletion of histories or shared work documents. Keep audit-only requests read-only and do not install or enable scheduled deletion merely because the skill supports recurring use.

Before editing, capture a reviewable diff and back up originals outside the private HOME, profile storage and every configured share, for example under `~/.local/share/fingerprint-terminal-maintenance/backups/`. Check the actual share sources before choosing that location. Preserve file permissions, verify the source has not changed since review, and use atomic replacement. Stop and rebase if a live process changed a target. Do not expose backup content in the isolated terminal or echo authentication material in reports.

For memory changes, update index summaries and links together. Keep these maintenance preferences in this skill; do not create a global `.claude/CLAUDE.md` to repeat them. Edit existing instruction files only when requested, and preserve the user's preferred conversation language.

For authorized conversation removal, reuse the provider-aware conversation manager's idle checks and index/sidecar handling. Describe when removal is reversible and when it is permanent. The helper has no mutation mode. Do not edit raw JSONL lines blindly or terminate a session without a request to do so.

After edits, verify the diff, frontmatter and memory links, and confirm the scope of files changed. Keep the `local` profile, host shell/SSH/Git configuration, configured shares and network gateway rules intact. Runtime timezone and locale remain tied to the verified exit. Do not claim browser fingerprint control or complete hardware isolation.

Recurring maintenance should use deterministic retention rules for explicitly selected caches; semantic memory changes remain an agent task with a readable diff and the same authorization boundaries.
