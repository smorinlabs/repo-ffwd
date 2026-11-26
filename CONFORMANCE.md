# CLI Standard Conformance — `repo-ffwd`

| | |
|---|---|
| **Standard** | CLI Design Standard v1.4.15 |
| **Profile** | Standard (noun-verb) |
| **Tier** | publishable |
| **Owner** | smorinlabs |

## Applicability

| Axis | Applies | Reason if N/A |
|---|---|---|
| Config (§5) | Yes | — |
| Networked (§10) | Yes | — |
| Destructive ops (§8) | Yes | — |
| Scripted consumers (R7.2/R7.8) | Yes | — |
| Async / long-running | Yes | — |
| Streaming / watch | No | Bounded batch repository sync; no continuous stream or watch mode |
| Plugins (R9.11) | No | Ships as an agent skill plugin, does not host a CLI plugin loader |
| Caching / offline (R5.9) | No | Reads live git repository state directly; no external response cache |
| Secrets handled (R5.5/R5.6) | No | Delegates git transport authentication to the user's git credential helper / SSH agent; never accepts or stores credentials |

## Waived SHOULDs

| Rule | Deviation | Rationale | Owner / date |
|---|---|---|---|

## Audit history

| Date | Standard version | Mode | Result |
|---|---|---|---|
| 2026-11-25 | 1.4.15 | plan | Interface spec (`docs/cli-interface.md`) and conformance note seeded prior to implementation |
