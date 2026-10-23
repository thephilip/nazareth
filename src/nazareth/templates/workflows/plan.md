Plan the next piece of work against this project's nazareth memory.

Focus: $focus

1. Call the nazareth `brief` tool to load the phase, constraints and active decisions.
2. If an architect lens is available (for example a `lens-architect` subagent), hand the
   rest of this to it. Otherwise do it yourself.
3. Read the focus: specs, an issue, or what the human described. For each design choice it
   settles, record it with `decide` (topic, the decision, and why). Where it changes an
   active decision, pass that decision's id in `supersedes`. Record choices and reasons,
   not the spec itself; the spec stays the source of the details.
4. For anything the material leaves open, or that conflicts with a constraint, `ask` the
   human instead of guessing.
5. If the phase no longer describes this work, suggest a new one to the human; they set it
   with `nazareth phase <current> --next <next>`.
6. Finish with a short summary: decisions recorded and superseded (with ids), questions
   asked, and anything now waiting on the human.
