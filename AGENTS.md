# Adversarial Friends

This repository ships a skill that challenges specs, plans, and reviews by
dispatching them to other agent CLIs as independent adversarial reviewers.

Read `skills/adversarial-friends/SKILL.md` for the workflow. Run the tool with
`bin/af run <artifact> --mode report`, and `bin/af doctor` when a run comes
back thinner than expected. `report` is the only mode this build implements;
see `skills/adversarial-friends/references/modes.md` for the rest.
