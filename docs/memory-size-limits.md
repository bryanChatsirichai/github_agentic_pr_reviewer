# Memory size limits

The reviewer keeps its memory of a PR (findings, their IDs and statuses) in a hidden block inside its own review comment. GitHub limits the size of a comment, so the memory is limited too. This note explains the limits, what happens when one is exceeded, and how the limits could be raised later.

## Where the memory lives

Each review posts **one comment**. The visible review and the hidden memory share it:

```
┌─ one GitHub comment (GitHub's limit: 65,536 chars) ─┐
│  Visible review         what people read            │
│  Hidden memory          our limit: 40,000 chars     │
└─────────────────────────────────────────────────────┘
```

- **Visible review:** the summary people read. It gets what the memory leaves, about 25,500 chars.
- **Hidden memory:** `<!-- ai-review-state:v1 {...} -->`, capped at 40,000 chars by `MAX_STATE_BLOCK_CHARS` in `orchestrator/memory.py`.

Each new comment holds the **full** memory so far, not just the changes. The next run reads only the newest bot comment:

```
Commit 1 → Comment #1   memory: F001, F002, F003
Commit 2 → Comment #2   memory: F001 … F005   (read from #1)
Commit 3 → Comment #3   memory: F001 … F005   (read from #2)
```

Memory grows with the number of findings the PR has ever had, not with the number of commits.

## Limits

| Limit | Value | Set by |
|---|---|---|
| Whole comment | 65,536 chars | GitHub |
| Hidden memory | 40,000 chars | `MAX_STATE_BLOCK_CHARS` (`orchestrator/memory.py`) |
| One finding's description | 1,000 chars, cut with `…` | `MAX_DESCRIPTION_CHARS` (`models/stored_finding.py`) |

A finding with a 600-char description takes about 1,000 chars of memory, so 40,000 holds about **38 findings**. With full 1,000-char descriptions it is about 28.

## What happens when a limit is exceeded

### Hidden memory over 40,000

```
memory too big?
   │
   ├─ yes → drop the oldest droppable finding (lowest ID) → check again
   │          │
   │          └─ nothing left to drop and still too big
   │                → StateTooLargeError
   │                → run fails (exit 1), NO comment posted
   │
   └─ no  → post the comment
```

- **Droppable:** `closed` and `rejected` (dismissed) findings.
- **Never dropped:** `open`, `reopened` and `resolved` (fixed) findings. Dropping one would make the next run report it again under a new ID.
- A dropped finding's ID is never reused (`next_id` is kept).
- If a dropped issue comes back later, it is reported as a new finding with a new ID.
- Posting nothing is on purpose: an empty or missing comment must never look like a clean review.

> **Today only dismissed findings can be dropped.** No code moves a finding to `verified` or `closed` yet (an open question in the design doc, section 12). Fixed findings therefore stay in memory for the life of the PR and count toward the 40,000 limit.

### Whole comment over 65,536

The code does not check the combined size of visible review plus memory. If it is too big, GitHub rejects the post and the run fails the same way: exit 1, no comment.

This is why the memory limit cannot simply be raised to 65,536. The visible review needs room in the same comment.

## Possible solutions for later

| # | Solution | Memory gained | Effort | Downside |
|---|---|---|---|---|
| 1 | **Shorter descriptions** through the reviewer prompt (aim for about 500 chars) | More findings fit in the same 40,000 | Small | Models don't count characters exactly; keep the 1,000-char cap as a backstop |
| 2 | **Compress the memory** (zlib + base64 inside the same hidden block) | About 2–3x (estimate; a synthetic test measured 4.5x) | Small: change save and load, keep reading old uncompressed blocks | Block is no longer human-readable when debugging |
| 3 | **Move the memory to its own hidden comment**, separate from the visible review | Up to about 65,000, and the visible review gets its own 65,536 | Medium: post and find two comments | The two posts can get out of sync if one fails |
| 4 | **Close fixed findings** (implement `resolved → verified → closed`, e.g. when the PR merges) | Fixed findings become droppable | Medium | Needs a decision on what "verified" means |
| 5 | **Check the combined size** before posting and shorten the visible review if needed | No more memory, but no failed post when the combined size is too big | Small | The visible review may be cut short |
| 6 | **Split the memory across several comments** | Practically unlimited | Large | More API calls; every part must load or memory is incomplete |
| 7 | **Store memory outside GitHub comments** (file on a branch, gist, database) | No limit | Large: permissions, setup | More moving parts and more ways to fail |

### Recommended order

1. **Shorter descriptions (1).** Cheapest, and comments become easier to read.
2. **Compression (2)**, if the memory ever overflows in real use.
3. **Separate memory comment (3)**, only if compression is still not enough.

Options 4 and 5 are independent fixes and can be done at any time. Options 6 and 7 cost more than this rare problem is worth.
