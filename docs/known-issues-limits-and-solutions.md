# Known issues, limits and potential solutions

Issues and limits found while testing the reviewer, why they happen, and what could fix them later. Each section ends with a recommended fix.

## Summary

| # | Issue | Severity | Status | Recommended fix |
|---|---|---|---|---|
| 1 | [Inline comments land on the wrong line](#1-inline-comments-land-on-the-wrong-line) | Major | Open | Number the diff lines before sending them to the model |
| 2 | [Hidden memory has a size limit](#2-hidden-memory-has-a-size-limit) | Minor (rare) | Accepted | Compress the memory if it ever overflows |
| 3 | [Long descriptions are cut off in inline comments](#3-long-descriptions-are-cut-off-in-inline-comments) | Minor | Mitigated | Done: higher cap and shorter descriptions in the prompt |

---

## 1. Inline comments land on the wrong line

### What happened

Test PR #12 (`test/dummy-review-bug`) added `sandbox/orders.py` with three planted bugs. The reviewer found all three and described the right fixes, but posted every inline comment above the bug:

| Finding | Reported line | Bug is on | Reported line holds |
|---|---|---|---|
| F001 Pagination skips the first page | 5 | 6 `start = page * page_size` | the docstring |
| F002 Empty orders divide by zero | 11 | 13 `return total / len(orders)` | the docstring |
| F003 Discount at the wrong scale | 15 | 18 `return price - price * percent` | a blank line |

The error grows the further down the file the bug is: 1, then 2, then 3 lines off.

### Why

The reviewer receives the raw unified diff (`render_pr_context` in `agents/reviewer_agents/factory.py`). To report a line, the model has to work out the new file's line numbers from the `@@` headers by counting lines. Language models count badly, so the number drifts.

This is a known problem for AI reviewers that post inline comments, not something specific to this project ([DEV](https://dev.to/shy_the_a91bfb236d4eeb5bb/why-ai-code-review-tools-keep-commenting-on-lines-that-dont-exist-ald), [GitLab #492104](https://gitlab.com/gitlab-org/gitlab/-/issues/492104), [OpenAI community](https://community.openai.com/t/comment-is-not-generated-on-proper-line-number-for-a-given-gitdiff/739669)).

### Impact

- **Placement.** The description names the code (`page * page_size`, `len(orders)`), so on a small file a reader still finds the bug. In a long file or large hunk the drift could reach tens of lines and point at unrelated code.
- **Memory.** The bot saves the code at the reported line as the finding's snippet and uses it to track the finding across commits and to re-check whether it is fixed. A snippet from a docstring or blank line may make those later checks go wrong. Not yet tested.
- **Posting.** In an edited file, a wrong line can fall outside the changed lines. GitHub refuses inline comments there, and in other tools one bad line has failed the whole review submission ([DEV](https://dev.to/shy_the_a91bfb236d4eeb5bb/i-thought-my-ai-code-reviewer-was-finished-then-a-single-hallucinated-line-number-broke-everything-2a55)). How this project handles that refusal is not yet checked.

### Potential solutions

| # | Fix | How it works | Effort | Downside |
|---|---|---|---|---|
| 1 | **Number the diff lines** | Prefix each diff line with its new-file line number, e.g. `  6 + start = page * page_size`, so the model copies the number instead of counting. The most common fix in other tools ([SSD Nodes](https://www.ssdnodes.com/learn/self-host-pr-review-agent), [pr-review-core](https://docs.rs/pr-review-core/0.14.0/pr_review_core/diff/)). | Small: one function that rewrites the diff, plus a prompt line | Slightly longer prompt; the model can still copy a nearby number, but rarely |
| 2 | **Have the model quote the code** | Add a field where the model quotes the exact line it means; the bot searches the file for it and uses the line where it is found. | Medium: new finding field and search logic | Fails if the line appears twice or the quote is altered; fall back to the reported number |
| 3 | **Check the line before posting** | If the line is outside the diff, blank, or only a comment or docstring, move it to the nearest changed code line or show the finding only in the summary ([Qodex](https://qodex.ai/docs/pr-review-inline-findings.md)). | Small to medium | Treats the symptom, not the cause |
| 4 | **Give the reviewer tools** | Let the model look up numbered file lines itself. Planned as Step 11 in the design doc. | Large | Waits on that step |

### Recommendation

Do **1** now and add **3** next. Fix 1 removes the cause and is small. Fix 3 catches what fix 1 misses and keeps a bad line from failing the post. Consider **2** only if fix 1 still gives wrong lines.

To verify: push the fix to a test PR like #12 and check that the comments land on lines 6, 13 and 18. Unit-test the numbering function with deleted lines (they don't advance the new file's numbering) and with several `@@` blocks.

---

## 2. Hidden memory has a size limit

### How memory is stored

The reviewer keeps its memory of a PR (findings, their IDs and statuses) in a hidden block inside its own review comment. Each review posts **one comment**, shared by the visible review and the hidden memory:

```
┌─ one GitHub comment (GitHub's limit: 65,536 chars) ─┐
│  Visible review         what people read            │
│  Hidden memory          our limit: 40,000 chars     │
└─────────────────────────────────────────────────────┘
```

- **Visible review:** the summary people read. It gets what the memory leaves, about 25,500 chars.
- **Hidden memory:** `<!-- ai-review-state:v1 {...} -->`, capped by `MAX_STATE_BLOCK_CHARS` in `orchestrator/memory.py`.

Each new comment holds the **full** memory so far, not just the changes. The next run reads only the newest bot comment:

```
Commit 1 → Comment #1   memory: F001, F002, F003
Commit 2 → Comment #2   memory: F001 … F005   (read from #1)
Commit 3 → Comment #3   memory: F001 … F005   (read from #2)
```

Memory grows with the number of findings the PR has ever had, not with the number of commits.

### Limits

| Limit | Value | Set by |
|---|---|---|
| Whole comment | 65,536 chars | GitHub |
| Hidden memory | 40,000 chars | `MAX_STATE_BLOCK_CHARS` (`orchestrator/memory.py`) |
| One finding's description | 1,000 chars, cut with `…` | `MAX_DESCRIPTION_CHARS` (`models/stored_finding.py`) |

A finding with a 600-char description takes about 1,000 chars of memory, so 40,000 holds about **38 findings**; with full 1,000-char descriptions, about 28. Normal PRs stay well below this.

The memory limit cannot simply be raised to 65,536: the visible review needs room in the same comment, and nothing checks their combined size.

### When the memory is over 40,000

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
- A dropped finding's ID is never reused (`next_id` is kept). If the issue comes back, it gets a new ID.
- Posting nothing is on purpose: an empty or missing comment must never look like a clean review.

> **Today only dismissed findings can be dropped.** No code moves a finding to `verified` or `closed` yet (an open question in the design doc, section 12). Fixed findings stay in memory for the life of the PR and count toward the limit.

### When the whole comment is over 65,536

The code does not check the combined size of visible review plus memory. If it is too big, GitHub rejects the post and the run fails the same way: exit 1, no comment.

### Potential solutions

| # | Fix | Memory gained | Effort | Downside |
|---|---|---|---|---|
| 1 | **Shorter descriptions** through the reviewer prompt (about 500 chars). Done, see issue 3. | More findings fit in the same 40,000 | Small | Models don't count characters exactly; the 1,000-char cap stays as a backstop |
| 2 | **Compress the memory** (zlib + base64 in the same hidden block) | About 2–3x (estimate; a synthetic test measured 4.5x) | Small: change save and load, keep reading old uncompressed blocks | Not human-readable when debugging |
| 3 | **Move the memory to its own hidden comment** | Up to about 65,000, and the visible review gets its own 65,536 | Medium: post and find two comments | The two posts can get out of sync if one fails |
| 4 | **Close fixed findings** (implement `resolved → verified → closed`, e.g. on merge) | Fixed findings become droppable | Medium | Needs a decision on what "verified" means |
| 5 | **Check the combined size** before posting and shorten the visible review if needed | None, but no failed post when the comment is too big | Small | The visible review may be cut short |
| 6 | **Split the memory across several comments** | Practically unlimited | Large | More API calls; every part must load |
| 7 | **Store memory outside GitHub comments** (file on a branch, gist, database) | No limit | Large: permissions, setup | More moving parts and failure modes |

### Recommendation

Overflow is rare, so no change is needed yet. If it happens in real use, add **compression (2)**; move to a **separate memory comment (3)** only if that is still not enough. Fixes 4 and 5 are independent and can be done at any time. Fixes 6 and 7 cost more than this problem is worth.

---

## 3. Long descriptions are cut off in inline comments

### What happened

An inline comment ended mid-sentence ("…Clear the…") and lost the suggested fix. The summary comment had the full text.

### Why

Descriptions are capped when saved to memory, to keep the memory small (issue 2). Inline comments are built from the saved copy, so they show the capped text. The summary comment uses the original, uncapped text.

### What was done

- Raised the cap from 300 to 1,000 chars (`MAX_DESCRIPTION_CHARS`) and the memory limit from 20,000 to 40,000 chars to match.
- Added rules to the reviewer prompt (`agents/reviewer_agents/prompts/logic-reviewer.md`): 2–4 sentences, about 500 chars, in the order what is wrong, its effect, the specific fix. On test PR #12, descriptions came out at about 150–250 chars, each ending with a fix.

### Remaining options

- **Build new inline comments from the full description** and keep the cap only for memory. This removes the cut-off entirely for first posts. A thread that has to be recreated later would still show the capped text, since only that is stored.
