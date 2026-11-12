# NECROMANCER – Peering & Consultation

**Peer first.** Peering is the main parallelization lever of this project; swarm/consult are secondary.

First check whether you are Parent or Sibling. If Sibling, load the skill `~/.pi/agent/skills/brainstorm/SKILL.md`.

- **`peer`** (sibling NECRO, own context + UI, returns only the final answer): for parallel work, own agent turns, (peer calls peer). Instances: see tool description (discovery `/tmp/necro`, status idle/busy).
- **`consult`** (bot-to-bot, max. 3 follow-ups, consultant only sees your answers): for expertise, reviews, second opinions. The consultant gets `~/.pi/agent/skills/brainstorm/SKILL.md` as context (the path is enough – swarm agents share the home).
  - "Good model" = large parameter count in the name (e.g. `-20B` = yes, `-2B` = no). This does not apply to peering: there I take the peer that is online.

**When NOT to peer/consult:** peer offline (tool error → retry later), task too small for the overhead, answer needed immediately (peer queue waits).

**Motivation:** A single agent is slow and one-sided. Two peers with different models find faster what one would overlook – parallel instead of sequential, and mutual control instead of self-deception.
