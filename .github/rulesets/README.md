# Rulesets

Importable GitHub repository rulesets. Import via **Settings → Rules → Rulesets →
New ruleset → Import** (or `gh api -X POST /repos/bryanChatsirichai/github_agentic_pr_reviewer/rulesets --input <file>`).

## main-branch-protection.json

Protects `refs/heads/main` on this public repo so that only the owner can commit,
merge, or rewrite history.

| Field / Rule | Value | Meaning |
|---|---|---|
| `target` | `branch` | Applies to branch refs only (use `tag` for a separate tag ruleset) |
| `enforcement` | `active` | Enforced. `disabled` = staged but off; `evaluate` = dry-run (logs violations without blocking) |
| `conditions.ref_name.include` | `refs/heads/main` | Only the `main` branch is covered |
| `deletion` rule | — | The branch cannot be deleted |
| `non_fast_forward` rule | — | No force pushes / history rewrites |
| `pull_request` rule | see below | All changes to `main` must go through a PR |
| ↳ `dismiss_stale_reviews_on_push` | `true` | New pushes invalidate old approvals |
| ↳ `required_approving_review_count` | `0` | No approvals required (solo repo) |
| ↳ `require_conversation_resolution` | `true` | All PR conversations must be resolved before merge |
| `bypass_actors` | user 89973472 | `bryanChatsirichai` (owner) may bypass everything — direct pushes, force pushes, deletion — in emergencies |

Note: "only the owner can commit/merge/delete" is already guaranteed by repo
permissions (public repo, no collaborators). This ruleset is insurance against
accidental force-pushes and deletions, including by the owner.

## Importing from Finder

`.github/` is a hidden dot-folder and won't appear in macOS file dialogs by
default — press **Cmd+Shift+.** in the dialog to show hidden folders. Or copy
the file somewhere visible first, or use the `gh api` command above.
