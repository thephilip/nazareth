Plan the next piece of work against this project's nazareth memory.

Focus: $focus

1. Call the nazareth `brief` tool to load the phase, constraints and active decisions.
2. If an architect lens is available (for example a `lens-architect` subagent), hand the
   rest of this to it. Otherwise do it yourself.
3. Read the focus: specs, an issue, or what the human described. For each design choice it
   settles, record it with `decide` (topic, the decision, and why). Where it changes an
   active decision, pass that decision's id in `supersedes`, or in `amends` if it changes
   only a detail. Keep each decision to a few sentences: record choices and reasons, not
   the spec itself; the spec stays the source of the details.
4. For anything the material leaves open, or that conflicts with a constraint, `ask` the
   human instead of guessing.
5. If the phase no longer describes this stage of work, set it with `phase`. A phase is a
   stage, not a release version or a backlog.
6. Finish with a short summary: decisions recorded and superseded (with ids), questions
   asked, and anything now waiting on the human.
