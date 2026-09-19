# Sentinel Loop
### A governed multi-agent task execution engine — plan, execute, verify, retry, escalate.

Sentinel Loop takes any open-ended text task, has a Manager agent break it into
an ordered set of sub-tasks, executes each with a Worker agent, and has a
Verifier agent independently judge every single output before it's allowed to
proceed — retrying with specific feedback on failure, escalating to a human
after repeated failure, and never letting an unverified claim reach the final
result.

---

## Why this exists

Most "AI agent" demos let a model act and hope for the best. Production
systems can't work that way — every non-trivial deployment needs oversight,
auditability, and a defined boundary where a human, not the model, has final
say. Sentinel Loop is built around that constraint from the ground up: no
step's output is trusted until a separate agent verifies it, and no task
completes without an explicit human approval checkpoint.

---

## Architecture

```mermaid
flowchart TD
    User([User Task]) --> Manager[Manager Agent]
    Manager --> Plan[Dynamic Sub-Task Plan]

    subgraph Loop [Per-Step Execution Loop]
        Plan --> Worker[Worker Agent]
        Worker --> RAG[Chroma RAG / Knowledge Base]
        Worker --> Tools[Controlled Tool Registry]
        Worker --> Output[Step Output]
        Output --> Verifier[Verifier Agent]
        Verifier -->|FAIL, attempts < 3| Retry[Retry with Feedback]
        Retry --> Worker
        Verifier -->|FAIL, attempts = 3| Escalate[Escalate to Human]
        Verifier -->|PASS| NextStep{More steps?}
        NextStep -->|Yes| Worker
    end

    NextStep -->|No| Synth[Synthesizer]
    Synth --> Approval[Awaiting Human Approval]
    Approval -->|Approve| Done([Completed])
    Approval -->|Request Changes| Revise[Re-synthesis]
    Revise --> Approval
    Loop -.-> Audit[(SQLite Audit Trail)]