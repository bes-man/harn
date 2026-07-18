# Required Tool Runtime Enforcement Design

## Invariant

A step cannot finish successfully until every declared required skill and tool has a matching usage event scoped to that task and step. The call may happen at any point during execution. Agent prose, web research, or use of a different tool cannot satisfy the requirement.

## Execution

- Audit usage when an agent turn attempts to complete.
- If required usage is missing after the first attempt, keep the step pending and run it once more.
- If it is still missing after the second attempt, mark the step blocked with the exact missing names.
- Apply the same completion gate to sequential workflow execution, parallel-wave members, and `run_step`.
- Recommended skills and tools remain observational only.

## Evidence

Usage is accepted only from `context_read` and `tool_used` events whose `task_id` and `step_id` match the executing step.

