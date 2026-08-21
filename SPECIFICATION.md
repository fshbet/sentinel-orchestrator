# UNIVERSAL AI ORCHESTRATION PLATFORM

## DOMAIN-AGNOSTIC, MCP-FIRST, INDUSTRY-GRADE MASTER BUILD SPECIFICATION

You are a senior systems architect, AI-agent architect, distributed-systems engineer, security engineer, protocol engineer, and software developer.

Your task is to research, design, implement, test, and document an **industry-grade Universal AI Orchestration Platform**.

This is NOT a framework for one programming language.

This is NOT a software-development-only framework.

This is NOT an OpenHands framework.

This is NOT a Claude framework.

This is NOT an Ollama framework.

This is NOT a collection of prompt files.

It is a **general-purpose orchestration platform for AI-driven work of arbitrary complexity**.

The system must be capable of receiving an arbitrary objective and dynamically determining:

- what needs to be done
- how it should be decomposed
- what capabilities are required
- what agents should perform the work
- what tools are required
- what MCP servers are required
- what sequence/dependencies exist
- what can run in parallel
- how results should be validated
- when work should be retried
- when another strategy should be attempted
- when human approval is required
- when the objective is actually complete

The system must NOT contain hard-coded assumptions about the domain of the task.

---

# 1. FUNDAMENTAL PRINCIPLE

The framework must follow:

> **Describe the goal, not the workflow.**

The user should be able to provide a high-level objective without specifying the implementation workflow.

Example:

```text
"Accomplish this objective."
```

The system must determine the appropriate execution strategy.

The user should NOT be required to specify:

- which agent to use
- how many agents to use
- which sequence to follow
- which tools to use
- which MCP servers to use
- how tasks should be decomposed
- which validation steps are required

unless the user explicitly wants to control those decisions.

---

# 2. NO DOMAIN-SPECIFIC ASSUMPTIONS

The core framework must NOT contain hard-coded workflows, agents, skills, validators, or logic for:

- specific programming languages
- specific databases
- specific BI platforms
- specific cloud platforms
- specific operating systems
- specific business domains
- specific project-management systems
- specific AI providers
- specific development environments

The core system must be domain-neutral.

Domain-specific capabilities must be implemented as:

- plugins
- skills
- MCP servers
- external tools
- project configuration
- optional workflow definitions

The core orchestrator must remain unaware of the domain.

---

# 3. RESEARCH BEFORE ARCHITECTURE

Before implementing the system, research current established approaches to AI orchestration.

Study and compare, where relevant:

- state-machine orchestration
- graph-based orchestration
- dynamic DAG generation
- orchestrator-worker
- planner-executor
- evaluator-optimizer
- router
- intent classification
- sequential workflows
- parallel workflows
- fan-out/fan-in
- hierarchical multi-agent systems
- swarm/handoff patterns
- event-driven orchestration
- durable execution
- workflow engines
- human-in-the-loop
- MCP-based tool orchestration
- agent registries
- capability registries
- context engineering
- memory/state management

Study established open-source implementations and standards where appropriate.

Examples may include, but are not limited to:

- LangGraph
- MCP ecosystem
- mcp-agent
- OpenHands architecture
- Temporal-style durable workflows
- other relevant open-source orchestration systems
- established multi-agent reference architectures

Do NOT blindly copy any framework.

Identify useful architectural patterns, limitations, tradeoffs, and gaps.

Create:

```text
docs/research/ORCHESTRATION_RESEARCH.md
```

This document must explain:

- approaches evaluated
- strengths
- weaknesses
- applicable patterns
- rejected approaches
- architectural decisions

---

# 4. ARCHITECTURAL GOAL

The final system should conceptually look like:

```text
                           USER OBJECTIVE
                                  │
                                  ▼
                         ┌─────────────────┐
                         │  GOAL ANALYZER  │
                         └────────┬────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │ CAPABILITY DISCOVERY│
                       └──────────┬──────────┘
                                  │
                                  ▼
                       ┌─────────────────────┐
                       │   TASK PLANNER      │
                       └──────────┬──────────┘
                                  │
                                  ▼
                         DYNAMIC TASK GRAPH
                                  │
                ┌─────────────────┼─────────────────┐
                ▼                 ▼                 ▼
              TASK A            TASK B            TASK C
                │                 │                 │
                ▼                 ▼                 ▼
              AGENT              AGENT              AGENT
                │                 │                 │
                └─────────────────┼─────────────────┘
                                  │
                                  ▼
                            RESULT SYNTHESIS
                                  │
                                  ▼
                             VALIDATION
                                  │
                         ┌────────┴────────┐
                         │                 │
                        PASS              FAIL
                         │                 │
                         ▼                 ▼
                     CONTINUE           RECOVERY
                                           │
                                           ▼
                                      NEW STRATEGY
                                           │
                                           ▼
                                      RE-EXECUTE
                                           │
                                           ▼
                                       VALIDATE
                                           │
                                           ▼
                                         REVIEW
                                           │
                                           ▼
                                       COMPLETE
```

This is conceptual only.

Choose the actual architecture after research.

---

# 5. COMBINE MULTIPLE ORCHESTRATION PARADIGMS

Do NOT force the entire system into one orchestration pattern.

The platform should support multiple composable patterns.

At minimum consider:

## Sequential

```text
A → B → C
```

## Parallel

```text
       ┌→ A ─┐
Start ─┼→ B ─┼→ Merge
       └→ C ─┘
```

## Router

```text
             ┌→ Strategy A
Goal → Router├→ Strategy B
             └→ Strategy C
```

## Orchestrator-Worker

```text
          Orchestrator
         /     |      \
       A       B       C
```

## Evaluator-Optimizer

```text
Generate
   ↓
Evaluate
   ↓
Pass?
 /   \
NO   YES
│     │
↓     ↓
Optimize Complete
│
└──→ Generate
```

## Dynamic DAG

```text
Goal
 ↓
Planner
 ↓
Task graph
 ↓
dependency-aware execution
```

## Hierarchical orchestration

```text
Root Orchestrator
       │
 ┌─────┼─────┐
 ▼     ▼     ▼
Sub   Sub   Sub
Org   Org   Org
```

## Handoff / swarm

Allow specialized agents to transfer responsibility when appropriate.

These patterns must be composable.

Do not force users to select the pattern manually unless they explicitly want to.

---

# 6. DYNAMIC PLANNING

The system must be capable of generating a task graph dynamically.

Example:

```text
Goal
 ↓
Analyze
 ↓
Task A
Task B
Task C
 ↓
Task D depends on A+B
Task E depends on C
 ↓
Task F depends on D+E
```

The planner determines:

- tasks
- dependencies
- ordering
- parallelism
- required capabilities
- expected outputs
- validation requirements

The resulting plan should be represented as structured data.

Do not rely exclusively on natural-language plans.

---

# 7. PLAN TYPES

Support at least:

## Full planning

Generate the complete plan before execution.

## Iterative planning

Plan the next meaningful step, execute it, inspect results, then plan again.

## Adaptive planning

Generate an initial plan but allow the orchestrator to modify it based on observations.

The system should select the appropriate strategy based on task complexity and configuration.

---

# 8. AGENT LOOP

The agent loop should be conceptually:

```text
OBSERVE
 ↓
REASON
 ↓
SELECT ACTION
 ↓
EXECUTE TOOL
 ↓
OBSERVE RESULT
 ↓
UPDATE STATE
 ↓
REASON AGAIN
```

However, the agent must NOT own global orchestration state.

The orchestration engine owns the workflow.

---

# 9. ORCHESTRATOR VS AGENT

This distinction is mandatory.

## Orchestrator

Responsible for:

- global state
- task graph
- dependencies
- scheduling
- agent selection
- capability selection
- tool authorization
- validation
- gates
- retries
- recovery
- human approval
- completion

## Agent

Responsible for:

- reasoning about assigned work
- using authorized tools
- producing outputs
- interpreting observations
- completing assigned tasks

## Validator

Responsible for:

- objective validation
- pass/fail evidence

## LLM

Responsible for:

- reasoning
- planning
- interpretation
- synthesis
- decision support

The LLM must NOT be the authoritative source of workflow state.

---

# 10. CAPABILITY-BASED ARCHITECTURE

Do not select agents primarily by hard-coded names.

Instead, define capabilities.

Example:

```yaml
capability:
  id: capability-name
  description: >
    Capability description

  requirements:
    tools: []
    skills: []
    permissions: []
```

An agent may advertise capabilities.

The orchestrator selects agents based on capability requirements.

This allows the system to dynamically discover appropriate workers.

---

# 11. AGENT REGISTRY

Create a dynamic agent registry.

Agents should register:

- identity
- capabilities
- description
- available tools
- skills
- constraints
- model
- permissions
- execution environment

Example conceptual structure:

```yaml
agent:
  id:
  capabilities:
  tools:
  skills:
  model:
  constraints:
  permissions:
```

Do not hard-code a fixed list of agent types.

---

# 12. DYNAMIC AGENT CREATION

The system should support dynamically defining a specialized agent for a task when appropriate.

For example:

```text
Objective
 ↓
Capability analysis
 ↓
Required role discovered
 ↓
Create/select suitable agent configuration
 ↓
Execute
```

Do not create unnecessary agents.

Agent creation must have a cost/benefit consideration.

---

# 13. SKILLS

Skills provide reusable knowledge or procedures.

Skills must be:

- modular
- discoverable
- versionable
- composable
- domain-independent at the core level

The framework must NOT ship with technology-specific assumptions as part of its core architecture.

Users may install optional skills later.

---

# 14. TOOL REGISTRY

Implement a unified tool registry.

Every tool should expose:

- ID
- name
- description
- input schema
- output schema
- capabilities
- permissions
- source
- timeout
- retry policy

Tool sources:

```text
builtin
native
plugin
MCP
adapter
```

---

# 15. MCP-FIRST INTEROPERABILITY

MCP is a first-class integration mechanism.

Use the current MCP specification and current official SDKs.

As of the current implementation target, the MCP ecosystem includes the 2026-07-28 specification, including a stateless protocol core, capability discovery, Tasks, extensions, authorization improvements, and cacheable list responses. Implement against the current stable specification available at development time rather than assuming an older protocol model.

MCP must provide interoperability with:

- external tools
- external services
- data sources
- agent capabilities
- future MCP-compatible systems

The framework should treat MCP as a standard capability boundary rather than inventing its own incompatible protocol.

---

# 16. MCP CLIENT

Implement MCP client capabilities including, as appropriate to the current specification:

- server discovery
- capability discovery
- tools
- resources
- prompts
- tasks
- authorization
- cancellation
- progress
- error handling
- pagination
- caching
- health checking

Do not implement deprecated protocol behavior merely because older MCP examples use it.

Follow the current official specification.

---

# 17. MCP SERVER SUPPORT

The framework should also be capable of exposing selected orchestration capabilities through MCP.

Potential capabilities may include:

- starting an execution
- querying execution status
- retrieving artifacts
- requesting approval
- inspecting workflow state

Do not expose dangerous administrative operations without explicit authorization.

---

# 18. MCP SECURITY

MCP must use least privilege.

A discovered MCP tool is NOT automatically trusted.

Lifecycle:

```text
DISCOVER
 ↓
DESCRIBE
 ↓
POLICY CHECK
 ↓
AUTHORIZE
 ↓
REGISTER
 ↓
USE
```

Support:

- server-level permissions
- tool-level permissions
- authentication
- authorization
- network policies
- timeout
- retry
- audit
- approval

---

# 19. LLM ABSTRACTION

Implement a provider-independent model interface.

Support:

- local models
- remote models
- OpenAI-compatible APIs
- Anthropic-compatible APIs
- future providers

The core orchestration engine must not depend on a specific provider.

---

# 20. MODEL CAPABILITIES

Models should advertise capabilities such as:

- text generation
- tool calling
- structured output
- vision
- long context
- streaming
- reasoning
- embeddings

The orchestrator should select models based on required capabilities rather than only model names.

---

# 21. MODEL FALLBACK

Support fallback strategies.

Example:

```text
Primary model
 ↓
failure?
 ↓
fallback model
 ↓
failure?
 ↓
alternate strategy
 ↓
human escalation
```

Do not endlessly retry the same failed model.

---

# 22. CONTEXT ENGINEERING

Context management must be a dedicated subsystem.

Do NOT send the entire available context to every model call.

Implement:

- context selection
- relevance ranking
- summarization
- compaction
- token budgeting
- context windows
- task context
- phase context
- artifact references
- state references
- history compression

The system must operate with models having small or large context windows.

---

# 23. CONTEXT BUDGET

Before a model call:

```text
Estimate context
 ↓
Compare against model capability
 ↓
Compress if necessary
 ↓
Remove low-value context
 ↓
Build final context
 ↓
Call model
```

The system must never knowingly exceed the configured model context limit.

---

# 24. MEMORY

Separate:

- execution state
- working memory
- long-term memory
- project knowledge
- artifacts
- audit history

Do not put everything into conversational history.

Memory should be selectively retrieved.

---

# 25. STATE MODEL

The system must have authoritative persistent state.

State should include:

```yaml
execution:
  id:
  status:
  objective:
  created_at:
  updated_at:

workflow:
  id:
  version:
  current_state:

tasks:
  - id:
    status:
    dependencies:
    assigned_agent:
    attempts:

artifacts:
  - id:
    type:
    location:

validations:
  - id:
    status:
    evidence:

failures:
  - id:
    category:
    message:
    recovery:

approvals:
  - id:
    status:
```

---

# 26. STATE MACHINE

Implement explicit state transitions.

Example:

```text
CREATED
 ↓
PLANNING
 ↓
READY
 ↓
RUNNING
 ↓
WAITING
 ↓
VALIDATING
 ↓
REVIEWING
 ↓
COMPLETED
```

Failure paths:

```text
RUNNING
 ↓
FAILED
 ↓
RECOVERING
 ↓
RUNNING
```

Invalid transitions must be rejected.

---

# 27. DURABLE EXECUTION

The architecture must support long-running work.

It must be possible to:

- pause
- resume
- retry
- recover
- cancel
- restart after process failure

Research whether a dedicated durable workflow engine such as Temporal should be supported as an optional execution backend.

Do NOT force a heavyweight backend for local development if it is unnecessary.

Provide an abstraction so durable execution can evolve independently.

---

# 28. IDEMPOTENCY

Operations should be idempotent where practical.

After interruption:

```text
resume
 ↓
inspect state
 ↓
identify completed operations
 ↓
avoid unnecessary duplication
 ↓
continue
```

Do not blindly repeat completed work.

---

# 29. TASK EXECUTION

Each task should contain:

- objective
- inputs
- expected outputs
- dependencies
- capabilities required
- assigned agent
- tools allowed
- validation requirements
- completion criteria

---

# 30. TASK DEPENDENCIES

Support:

```text
A → B
A + B → C
C → D
```

Detect invalid dependency graphs.

Detect cycles unless cycles are explicitly represented as controlled iterative loops.

---

# 31. PARALLEL EXECUTION

Allow independent tasks to execute concurrently.

The scheduler must consider:

- dependencies
- shared resources
- file conflicts
- permissions
- concurrency limits
- tool constraints

---

# 32. RESOURCE LOCKING

Where necessary, implement resource locking.

Examples of resources:

- files
- repositories
- databases
- external systems
- environments

Avoid race conditions between agents.

---

# 33. VALIDATION

Validation must be independent from agent claims.

Validators may use:

- commands
- tests
- schemas
- APIs
- MCP tools
- external systems
- deterministic checks
- custom validators

A validator must produce structured evidence.

---

# 34. EVIDENCE-BASED COMPLETION

Never allow:

```text
LLM says:
"Done."
```

to be equivalent to:

```text
VALIDATION:
PASS
```

Completion should require evidence appropriate to the task.

For tasks where deterministic validation is impossible, use an explicit evaluation strategy and clearly mark uncertainty.

---

# 35. EVALUATOR-OPTIMIZER

Implement an evaluator-optimizer pattern.

```text
OUTPUT
 ↓
EVALUATE
 ↓
PASS?
 / \
NO YES
│   │
↓   ↓
OPTIMIZE COMPLETE
│
└──→ EVALUATE
```

Support configurable evaluation criteria.

---

# 36. RECOVERY ENGINE

Failures should be treated as data.

When something fails:

```text
FAILURE
 ↓
CLASSIFY
 ↓
ANALYZE
 ↓
SELECT RECOVERY STRATEGY
 ↓
EXECUTE
 ↓
VALIDATE
```

Failure categories may include:

- transient
- tool
- MCP
- model
- context
- dependency
- permission
- validation
- execution
- logical
- unknown

Do not hard-code recovery for only software-development failures.

---

# 37. RECOVERY STRATEGIES

Possible strategies:

- retry
- modify parameters
- use alternate tool
- use alternate agent
- use alternate model
- re-plan
- rollback
- reduce scope
- request human input
- terminate safely

The orchestrator should choose based on failure type and policy.

---

# 38. HUMAN-IN-THE-LOOP

Human intervention must be a first-class orchestration state.

Support:

```text
WAITING_FOR_APPROVAL
WAITING_FOR_INPUT
WAITING_FOR_CREDENTIAL
WAITING_FOR_DECISION
```

The workflow must resume after the required information is supplied.

---

# 39. RISK ENGINE

Implement risk classification.

Classify operations by:

- reversibility
- impact
- permissions
- external effects
- data sensitivity
- financial impact
- security impact

High-risk operations may require human approval.

Do not use "production" as the only risk boundary.

---

# 40. POLICY ENGINE

Implement a policy system.

Policies can govern:

- tools
- MCP servers
- agents
- models
- workflows
- filesystem
- network
- external systems
- sensitive operations

Policies must be configurable.

---

# 41. AUDIT

Record:

- execution
- task
- phase
- agent
- model
- tool
- MCP server
- MCP tool
- decision
- validation
- failure
- recovery
- approval

Use structured audit events.

Never log secrets.

---

# 42. OBSERVABILITY

Expose:

- current execution
- task graph
- current task
- active agents
- model
- tool calls
- MCP calls
- validation
- failures
- retries
- approvals
- artifacts

Support structured JSON logs.

Design for future metrics/tracing integration.

---

# 43. AGENT COMMUNICATION

Agents should communicate through:

- structured task results
- artifacts
- state
- explicit messages

Do not depend exclusively on conversational context.

---

# 44. AGENT ISOLATION

Where practical, agents should have:

- scoped context
- scoped tools
- scoped permissions
- scoped MCP servers
- scoped resources

A worker should not automatically inherit every capability available to the root orchestrator.

---

# 45. AGENT HIERARCHY

Support:

```text
Root Orchestrator
       │
       ├── Sub-Orchestrator
       │      ├── Worker
       │      └── Worker
       │
       └── Worker
```

Nested orchestration must be possible.

---

# 46. PLUGIN ARCHITECTURE

The system must support plugins for:

- capabilities
- agents
- tools
- validators
- workflows
- model providers
- execution engines
- storage
- observability
- MCP integrations

Plugins must not require modification of the core engine.

---

# 47. ADAPTER ARCHITECTURE

Support adapters for:

```text
LLM
Execution
Storage
Tools
Observability
Workflow backend
```

Examples:

```text
LLM:
Ollama
OpenAI-compatible
Anthropic-compatible

Execution:
OpenHands
Claude Code
Generic Agent
Custom Worker

Storage:
SQLite
PostgreSQL
Other supported backend

Workflow:
Local
Durable workflow engine
```

These are examples only.

The architecture must remain open-ended.

---

# 48. OPENHANDS INTEGRATION

OpenHands should be implemented as an optional execution adapter.

The architecture should allow:

```text
Universal Orchestrator
 ↓
OpenHands Adapter
 ↓
OpenHands
```

The orchestrator should provide OpenHands with:

- objective
- task
- relevant context
- allowed tools
- required capabilities
- validation requirements
- completion criteria

The adapter should return structured results.

Do not duplicate OpenHands' internal agent functionality.

---

# 49. CLI

Provide a CLI.

Core commands should include:

```bash
orchestrator init
orchestrator run "<objective>"
orchestrator status
orchestrator pause <id>
orchestrator resume <id>
orchestrator cancel <id>
orchestrator inspect <id>
orchestrator workflows
orchestrator agents
orchestrator capabilities
orchestrator tools
orchestrator mcp
orchestrator models
orchestrator validate
```

Exact command structure may be improved during implementation.

---

# 50. REST API

Provide an optional API for external integrations.

At minimum:

```text
POST /executions
GET /executions
GET /executions/{id}
POST /executions/{id}/pause
POST /executions/{id}/resume
POST /executions/{id}/cancel

GET /workflows
GET /agents
GET /capabilities
GET /tools
GET /mcp
GET /models

GET /health
GET /metrics
```

Version the API.

---

# 51. CONFIGURATION

Configuration hierarchy:

```text
Built-in defaults
 ↓
Global configuration
 ↓
User configuration
 ↓
Project configuration
 ↓
Workflow configuration
 ↓
Task configuration
```

Later configuration overrides earlier configuration.

---

# 52. PROJECT CONFIGURATION

A project may optionally contain:

```text
.orchestrator/
```

but the project must not need to copy the entire framework.

Example:

```text
project/
├── .orchestrator/
│   ├── config.yaml
│   ├── policies.yaml
│   └── optional-overrides/
└── project files
```

The framework should be installable centrally and reusable across projects.

---

# 53. NO REQUIRED PROJECT TYPE

The system must work even when the project is:

- unfamiliar
- mixed technology
- non-code
- partially structured
- newly created
- an existing repository
- a collection of documents
- an external system
- a research problem
- an arbitrary task

Do not assume a repository even exists.

---

# 54. GOAL UNDERSTANDING

The first stage should determine:

- objective
- desired outcome
- constraints
- available resources
- environment
- risks
- ambiguity
- required capabilities
- success criteria

If the objective is sufficiently clear, continue without unnecessary questions.

If critical information is missing, request clarification.

Do not ask unnecessary questions merely because information could theoretically be useful.

---

# 55. REQUIREMENT EXTRACTION

Convert natural language into structured requirements.

Separate:

```text
Explicit requirements
Inferred requirements
Assumptions
Constraints
Unknowns
Success criteria
```

Never silently convert an assumption into a requirement.

---

# 56. PLANNING STRATEGY

The planner should determine:

- whether planning should be full or iterative
- whether parallelism is beneficial
- whether specialized agents are required
- whether MCP capabilities are required
- whether external research is required
- whether human approval is required
- what validation is possible
- what evidence is required

---

# 57. SELF-ADAPTATION

The orchestration system should adapt to:

- task complexity
- model capabilities
- available tools
- available agents
- available MCP servers
- environment
- risk
- context limits
- execution failures

Do not use a fixed workflow for every task.

---

# 58. SIMPLE TASK OPTIMIZATION

Do not over-engineer trivial tasks.

For a simple task:

```text
Goal
 ↓
Agent
 ↓
Tool
 ↓
Validation
 ↓
Done
```

is preferable to creating:

```text
10 agents
15 phases
4 reviewers
```

The orchestrator must balance reliability with unnecessary complexity.

---

# 59. COMPLEX TASK OPTIMIZATION

For complex tasks, the system may dynamically introduce:

- planners
- sub-planners
- workers
- researchers
- evaluators
- reviewers
- recovery agents

The number of agents should be determined by actual task requirements.

---

# 60. COST / RESOURCE AWARENESS

The orchestrator should consider:

- token usage
- model cost where available
- execution time
- tool latency
- concurrency
- resource availability

Prefer efficient execution when quality is not compromised.

For local models, consider:

- context size
- VRAM/RAM constraints
- model availability
- inference latency

---

# 61. MODEL ROUTING

Model routing should be capability-based.

Example conceptual decision:

```text
Task requires:
long context + tool calling

Available models:
A: long context + tools
B: short context
C: no tools

Select A.
```

Do not hard-code model names into orchestration logic.

---

# 62. SECURITY BOUNDARIES

Separate:

```text
Reasoning
Execution
Authorization
Validation
```

The model can request an action.

The policy engine decides whether the action is allowed.

The tool executes the authorized action.

The validator evaluates the result.

---

# 63. EXECUTION SANDBOX

Where supported, provide execution isolation.

Potential isolation levels:

```text
none
restricted
sandbox
container
remote worker
```

The framework must not assume Docker or another specific sandbox technology.

---

# 64. RESOURCE GOVERNANCE

Support limits for:

- execution time
- tool calls
- model calls
- retries
- parallel workers
- memory
- tokens
- external requests

The limits must be configurable.

---

# 65. CANCELLATION

Cancellation should be graceful.

Example:

```text
RUNNING
 ↓
CANCELLING
 ↓
SAFE STOP
 ↓
STATE PERSISTED
 ↓
CANCELLED
```

---

# 66. RESUME

After restart:

```text
LOAD STATE
 ↓
VERIFY STATE
 ↓
IDENTIFY LAST SAFE POINT
 ↓
RECONSTRUCT REQUIRED CONTEXT
 ↓
RESUME
```

Do not restart the entire task unnecessarily.

---

# 67. IDEMPOTENCY

Track operation IDs where practical.

Avoid duplicated external side effects after retries.

---

# 68. WORKFLOW VERSIONING

Workflows must be versioned.

An execution must retain the workflow version used when it started.

Do not silently change a running execution to a newer workflow definition.

---

# 69. AGENT VERSIONING

Where appropriate, retain the version of:

- agent definition
- skill
- workflow
- policy
- model configuration

used during execution.

This improves reproducibility.

---

# 70. REPRODUCIBILITY

An execution should be reconstructable from:

- objective
- workflow version
- agent versions
- configuration
- model/provider
- tool versions
- MCP server information
- artifacts
- state
- audit events

---

# 71. TESTING

Implement:

- unit tests
- integration tests
- workflow tests
- state tests
- MCP tests
- adapter tests
- security tests
- recovery tests
- concurrency tests
- context tests
- CLI tests
- API tests
- end-to-end tests

---

# 72. TEST THE ORCHESTRATOR ITSELF

Do not test only example applications.

Test orchestration behavior.

At minimum:

1. simple objective
2. complex objective
3. ambiguous objective
4. dynamic task decomposition
5. sequential execution
6. parallel execution
7. dependency resolution
8. dynamic agent selection
9. tool discovery
10. MCP discovery
11. MCP tool execution
12. MCP permission denial
13. model failure
14. tool failure
15. validation failure
16. recovery
17. repeated failure
18. model fallback
19. context compaction
20. human approval
21. pause
22. resume
23. cancellation
24. workflow versioning
25. concurrent execution
26. interrupted execution
27. invalid workflow
28. invalid configuration
29. security violation
30. completion verification

---

# 73. PROPERTY / INVARIANT TESTING

Where practical, test system invariants such as:

- invalid state transitions never occur
- completed tasks are not duplicated unnecessarily
- unauthorized tools cannot execute
- failed mandatory gates cannot produce COMPLETED
- cancelled executions cannot silently continue
- invalid workflows are rejected
- execution state survives restart

---

# 74. FAILURE INJECTION

Implement controlled failure tests.

Simulate:

- model timeout
- MCP timeout
- tool failure
- network failure
- malformed response
- validation failure
- process interruption
- corrupted temporary state

Verify recovery behavior.

---

# 75. DOCUMENTATION

Create:

```text
docs/

architecture.md
architecture-decisions.md
research.md
getting-started.md
installation.md
configuration.md
orchestration.md
planning.md
agents.md
capabilities.md
tools.md
mcp.md
models.md
context.md
memory.md
state.md
validation.md
recovery.md
security.md
policies.md
adapters.md
durable-execution.md
cli.md
api.md
plugins.md
extending.md
troubleshooting.md
examples.md
```

---

# 76. ARCHITECTURE DECISION RECORDS

Create ADRs for major decisions.

Examples:

```text
ADR-001 Core orchestration model
ADR-002 Dynamic planning
ADR-003 MCP integration
ADR-004 State persistence
ADR-005 Durable execution
ADR-006 Agent registry
ADR-007 Context management
ADR-008 Validation architecture
ADR-009 Security model
ADR-010 Adapter architecture
```

Do not create ADRs merely for trivial decisions.

---

# 77. EXTERNAL RESEARCH POLICY

When the orchestrator needs external knowledge, it should distinguish:

```text
Known
Retrieved
Inferred
Assumed
Unverified
```

Where external evidence is important, preserve references/evidence.

---

# 78. EVIDENCE MODEL

Results should be able to contain:

```yaml
evidence:
  type:
  source:
  location:
  confidence:
  timestamp:
```

This enables future research and verification workflows.

---

# 79. NO FAKE IMPLEMENTATION

Do not create:

- fake success
- placeholder production logic
- hard-coded pass results
- fake MCP implementations
- fake model providers
- empty methods for required functionality

Mocks are permitted only for appropriate isolated tests.

---

# 80. DEPENDENCY MANAGEMENT

Keep dependencies minimal.

Separate:

- core dependencies
- optional integrations
- development dependencies

A user who does not use a particular adapter must not be forced to install it.

---

# 81. CROSS-PLATFORM

Support:

- Windows
- Linux
- macOS

Avoid platform-specific assumptions.

---

# 82. STORAGE ABSTRACTION

Abstract persistence.

Initial local implementation may use an embedded database.

The interface should allow future:

- PostgreSQL
- distributed storage
- cloud storage

without rewriting the orchestration engine.

---

# 83. OBSERVABILITY EXTENSIBILITY

Design for future:

- metrics
- tracing
- dashboards
- OpenTelemetry-compatible instrumentation

Do not make observability a hard dependency for basic operation.

---

# 84. API DESIGN

Use:

- explicit schemas
- versioned APIs
- validation
- structured errors
- backward-compatible evolution

Document public APIs.

---

# 85. CLI DESIGN

The CLI should provide useful human-readable output but also support machine-readable output such as:

```bash
--json
```

where appropriate.

---

# 86. CONFIGURATION VALIDATION

Validate:

- syntax
- schema
- references
- permissions
- workflows
- agents
- tools
- MCP servers
- models

before execution.

---

# 87. WORKFLOW VALIDATION

Before executing a workflow:

- verify agents
- verify capabilities
- verify tools
- verify validators
- verify gates
- verify dependencies
- detect invalid cycles
- verify permissions
- verify model requirements

Reject invalid workflows.

---

# 88. MCP HEALTH

Provide MCP health/status functionality.

Show:

- server
- status
- transport
- capabilities
- available tools
- authorization state
- latency
- errors

---

# 89. MODEL HEALTH

Provide model/provider health.

Show:

- provider
- model
- availability
- capabilities
- context size when available
- tool calling support
- structured output support
- latency where measurable

---

# 90. EXAMPLE WORKFLOWS

Do NOT create examples tied to specific technologies.

Instead create generic examples:

### Example A — Research

```text
Goal
 ↓
Research
 ↓
Evidence collection
 ↓
Evaluation
 ↓
Synthesis
 ↓
Review
```

### Example B — Creation

```text
Goal
 ↓
Plan
 ↓
Create
 ↓
Validate
 ↓
Review
 ↓
Deliver
```

### Example C — Problem solving

```text
Problem
 ↓
Diagnose
 ↓
Hypothesis
 ↓
Experiment
 ↓
Evaluate
 ↓
Correct
 ↓
Verify
```

### Example D — Complex project

```text
Goal
 ↓
Decompose
 ↓
Parallel tasks
 ↓
Merge
 ↓
Validate
 ↓
Review
 ↓
Iterate
 ↓
Complete
```

These are architectural examples only.

---

# 91. DO NOT BUILD A FIXED AGENT LIBRARY

Do not make the architecture depend on:

```text
developer
tester
researcher
database-agent
powerbi-agent
etc.
```

as mandatory components.

These may exist as examples or optional agents.

The core system must use **capabilities and dynamically selected roles**.

---

# 92. DO NOT BUILD A FIXED WORKFLOW LIBRARY

Do not make:

```text
software-development.yaml
database.yaml
powerbi.yaml
etc.
```

mandatory.

The core must support dynamically generated workflows.

Predefined workflows may be included only as generic patterns:

- sequential
- parallel
- router
- evaluator-optimizer
- orchestrator-worker
- hierarchical
- iterative
- dynamic DAG

---

# 93. META-ORCHESTRATION

The system itself should be capable of selecting the orchestration pattern.

Example:

```text
Objective
 ↓
Analyze complexity
 ↓
Select pattern
 ↓
Build workflow
 ↓
Execute
```

For a simple task:

```text
single agent
```

For a complex task:

```text
planner
 ↓
dynamic workers
 ↓
evaluator
 ↓
recovery
```

---

# 94. ORCHESTRATION PATTERN SELECTION

The system should consider:

- complexity
- dependencies
- uncertainty
- parallelism
- risk
- required expertise
- available capabilities
- validation requirements
- execution cost

Then select or compose orchestration patterns.

---

# 95. SELF-REFLECTION SHOULD NOT BE UNBOUNDED

Allow evaluation and re-planning, but impose:

- retry limits
- time limits
- token limits
- cost limits
- iteration limits

Prevent infinite agent loops.

---

# 96. COMPLETION

The system may declare:

```text
COMPLETED
```

only when the defined success criteria have sufficient evidence.

For deterministic tasks, prefer deterministic validation.

For subjective tasks, use evaluator agents and confidence/evidence reporting.

---

# 97. UNCERTAINTY

The framework should be able to report:

```text
CONFIRMED
LIKELY
UNCERTAIN
BLOCKED
FAILED
```

Do not force false certainty.

---

# 98. HUMAN ESCALATION

If:

- repeated recovery fails
- required information is missing
- risk exceeds policy
- confidence is too low
- external dependency is unavailable

the system should escalate rather than blindly continue.

---

# 99. INDUSTRY-GRADE QUALITY

The implementation must emphasize:

- correctness
- reliability
- security
- maintainability
- extensibility
- observability
- interoperability
- recoverability
- testability
- portability

Do not add unnecessary complexity merely to appear enterprise-grade.

Prefer simple mechanisms where they are sufficient.

---

# 100. IMPLEMENTATION STRATEGY

Do NOT implement the complete system in one uncontrolled pass.

Implement in stages.

## Stage 1 — Research and architecture

Create:

- research
- architecture
- ADRs

Do not implement yet.

## Stage 2 — Core domain model

Implement:

- execution
- task
- workflow
- phase
- state
- artifact

## Stage 3 — Workflow engine

Implement:

- transitions
- dependencies
- dynamic DAG
- parallelism
- branching
- loops

## Stage 4 — Agent/capability system

Implement:

- agent registry
- capability registry
- dynamic selection
- scoped permissions

## Stage 5 — LLM abstraction

Implement:

- provider interface
- model capability discovery
- model routing
- fallback

## Stage 6 — Tool abstraction

Implement:

- tool registry
- native tools
- permissions

## Stage 7 — MCP

Implement current MCP support.

Do not implement an outdated MCP protocol model.

Use the current official MCP specification and official SDKs.

## Stage 8 — Context/memory

Implement:

- context budgeting
- relevance
- compaction
- memory
- artifact references

## Stage 9 — Validation/gates

Implement:

- validators
- evidence
- gates
- evaluator-optimizer

## Stage 10 — Recovery

Implement:

- failure classification
- recovery strategies
- retries
- escalation

## Stage 11 — Human-in-the-loop

Implement:

- approvals
- questions
- pause/resume

## Stage 12 — Durable execution

Implement local persistence first.

Add an optional durable workflow backend abstraction.

## Stage 13 — Adapters

Implement:

- generic execution adapter
- OpenHands adapter
- additional adapters only where useful

## Stage 14 — CLI

Implement CLI.

## Stage 15 — API

Implement REST API.

## Stage 16 — Security/observability

Implement:

- policy engine
- audit
- structured logs
- instrumentation

## Stage 17 — End-to-end testing

Test the complete orchestration system.

---

# 101. RESEARCH-DRIVEN DESIGN

Do not assume that the architecture described in this prompt is automatically optimal.

Before implementing each major subsystem:

1. Research established approaches.
2. Compare alternatives.
3. Select the simplest architecture that satisfies the requirements.
4. Record the decision.
5. Implement it.
6. Test it.

If a better architecture is discovered during research, change the design.

Do not follow this prompt blindly when evidence indicates a better solution.

---

# 102. IMPORTANT: DO NOT OVERFIT TO THE USER

The framework must NOT be designed around:

- the user's current projects
- the user's programming languages
- the user's current hardware
- the user's current AI tools
- the user's current development environment

The framework must be genuinely general-purpose.

Optional adapters may support these environments later.

---

# 103. IMPORTANT: DO NOT OVERFIT TO OPENHANDS

OpenHands is an integration target.

Do not reproduce OpenHands internally.

Do not make OpenHands mandatory.

Do not make OpenHands concepts part of the core domain model unless they represent a genuinely universal orchestration concept.

---

# 104. IMPORTANT: DO NOT OVERFIT TO MCP

MCP is the preferred interoperability protocol, but the core orchestration engine must remain conceptually independent from MCP.

MCP is a tool/capability boundary.

It is not the workflow engine itself.

---

# 105. IMPORTANT: DO NOT OVERFIT TO MARKDOWN

Markdown is useful for:

- agent definitions
- skill definitions
- policies
- documentation
- human-readable artifacts

But the orchestration engine must be executable software.

Do not implement the system as a giant collection of Markdown prompts.

---

# 106. IMPORTANT: DO NOT OVERFIT TO LLM REASONING

Do not let the LLM control everything.

Use deterministic software for:

- state
- scheduling
- permissions
- retries
- persistence
- validation
- resource limits
- workflow transitions

Use LLMs for:

- reasoning
- interpretation
- planning
- synthesis
- judgment where appropriate

This separation is fundamental.

---

# 107. FINAL DIRECTORY STRUCTURE

Create an architecture approximately like:

```text
universal-orchestrator/

├── core/
│   ├── domain/
│   ├── state/
│   ├── workflow/
│   ├── scheduler/
│   ├── execution/
│   └── policy/
│
├── planning/
│   ├── goal/
│   ├── decomposition/
│   ├── graph/
│   └── strategy/
│
├── agents/
│   ├── registry/
│   ├── capabilities/
│   ├── selection/
│   └── runtime/
│
├── llm/
│   ├── providers/
│   ├── routing/
│   └── capabilities/
│
├── tools/
│   ├── registry/
│   ├── native/
│   └── permissions/
│
├── mcp/
│   ├── client/
│   ├── discovery/
│   ├── registry/
│   ├── policy/
│   └── transport/
│
├── context/
│   ├── manager/
│   ├── compaction/
│   ├── retrieval/
│   └── memory/
│
├── validation/
│   ├── validators/
│   ├── gates/
│   └── evidence/
│
├── recovery/
│   ├── classification/
│   ├── strategies/
│   └── retry/
│
├── adapters/
│   ├── execution/
│   ├── storage/
│   ├── workflow/
│   └── observability/
│
├── api/
├── cli/
├── plugins/
├── policies/
├── workflows/
├── examples/
├── tests/
├── docs/
└── config/
```

Modify this structure if research demonstrates a better architecture.

---

# 108. FINAL TESTING REQUIREMENT

After implementation:

1. Run unit tests.
2. Run integration tests.
3. Run MCP tests.
4. Run adapter tests.
5. Run state/recovery tests.
6. Run concurrency tests.
7. Run security tests.
8. Run end-to-end tests.
9. Inject failures.
10. Test recovery.
11. Test interruption/resume.
12. Test context limitations.
13. Test dynamic planning.
14. Test dynamic agent selection.
15. Test MCP tool selection.
16. Test validation gates.
17. Test human approval.
18. Test cancellation.
19. Test workflow versioning.

Fix all failures.

Run the complete suite again.

---

# 109. FINAL REVIEW

Perform an independent architecture review.

Ask:

- Is the system genuinely domain agnostic?
- Is the core independent of OpenHands?
- Is the core independent of any LLM?
- Is MCP properly integrated?
- Is state authoritative?
- Can workflows be generated dynamically?
- Can agents be dynamically selected?
- Can tasks execute in parallel?
- Can the system recover?
- Can execution resume?
- Can dangerous actions be controlled?
- Can the system handle simple tasks efficiently?
- Can it handle complex tasks?
- Can it support future orchestration patterns?
- Are there unnecessary hard-coded assumptions?
- Are there unnecessary abstractions?
- Are there single points of failure?
- Is the system observable?
- Is it testable?
- Is it maintainable?

Fix all significant issues discovered.

---

# 110. FINAL ARTIFACTS

Create:

```text
FINAL_REPORT.md
ARCHITECTURE.md
ORCHESTRATION_GUIDE.md
MCP_GUIDE.md
EXTENDING.md
TEST_REPORT.md
```

The final report must include:

- architecture
- research findings
- major decisions
- implemented components
- orchestration patterns
- dynamic planning
- agent system
- capability system
- MCP
- LLM abstraction
- context management
- state management
- validation
- recovery
- security
- adapters
- OpenHands integration
- CLI
- API
- tests
- limitations
- future roadmap

---

# 111. FINAL ACCEPTANCE CRITERIA

The framework is complete only when:

- it is domain agnostic
- it does not require predefined project types
- it does not require predefined agent roles
- it does not require predefined workflows for every domain
- it supports dynamic task decomposition
- it supports dynamic task graphs
- it supports multiple orchestration patterns
- it supports dynamic agent selection
- it supports capability-based routing
- it supports MCP
- it supports model abstraction
- it supports context management
- it supports persistent state
- it supports recovery
- it supports validation
- it supports human approval
- it supports concurrency
- it supports cancellation
- it supports resume
- it supports adapters
- it supports OpenHands as an optional adapter
- it provides a CLI
- it provides an API
- it provides auditability
- it provides observability
- it is tested
- it is documented
- it contains no required fake implementations
- it can be installed once and reused across arbitrary projects

---

# 112. FINAL INSTRUCTION

Build this as a **general-purpose AI orchestration platform**, not as a collection of examples tailored to one developer.

The central philosophy is:

```text
                 ANY OBJECTIVE
                       │
                       ▼
                UNDERSTAND GOAL
                       │
                       ▼
             DISCOVER CAPABILITIES
                       │
                       ▼
              DYNAMICALLY PLAN
                       │
                       ▼
              BUILD TASK GRAPH
                       │
                       ▼
              SELECT CAPABILITIES
                       │
                       ▼
             EXECUTE WITH AGENTS
                       │
                       ▼
                  VALIDATE
                       │
                ┌──────┴──────┐
                │             │
               PASS          FAIL
                │             │
                ▼             ▼
              REVIEW       RECOVER
                │             │
                │             ▼
                │          RE-PLAN
                │             │
                │             ▼
                │          RE-EXECUTE
                │             │
                │             ▼
                │          VALIDATE
                │             │
                └──────┬──────┘
                       ▼
                    COMPLETE
```

The system should be able to adapt this architecture to **whatever objective it receives**.

Do not constrain the framework based on examples.

Do not constrain it based on the developer's current knowledge.

Do not constrain it based on current AI tools.

Do not constrain it based on one model.

Research better approaches when they exist.

Prefer established standards.

Prefer composable architecture.

Prefer capability discovery over hard-coded roles.

Prefer dynamic planning over fixed workflows.

Prefer deterministic control for state, security, validation, and execution.

Prefer LLM reasoning only where reasoning is actually required.

The final result should be a **domain-agnostic, MCP-first, industry-grade Universal AI Orchestration Platform capable of evolving as AI agents, models, tools, protocols, and execution environments evolve.**