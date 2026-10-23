---
name: nazareth-review
description: "Review the current change against recorded decisions and constraints before it is pushed, logging findings with a severity."
argument-hint: [focus]
---

Review the current change against this project's nazareth memory before it is pushed.

Focus: $ARGUMENTS

(If no focus is given above: uncommitted changes plus commits not yet pushed (`git diff`, `git log @{u}..`).)

1. Call the nazareth `brief` tool to load the constraints and active decisions.
2. If a reviewer lens is available (for example a `lens-reviewer` subagent), hand the rest
   of this to it. Otherwise do it yourself. Review only: don't edit code.
3. Check the change against every relevant active decision and constraint, then for
   correctness and security. Use `query` to look up decisions in the areas it touches.
4. `log` each problem as a `finding` with a severity (low, medium, high, critical) and a
   `ref` such as `src/file.ts:42`. If there are none, `log` one `review` event saying what
   you checked, so the human can see the change was reviewed.
5. Report the findings by severity. If a recorded decision looks wrong rather than the
   code, say so and `ask` the human instead of working around it.
