<p align="center">
  <img src="images/banner.svg" alt="AI PR Reviewer banner" width="100%"/>
</p>

# AI GitHub PR Code Reviewer

I built an event-driven code review system that automatically reviews GitHub pull requests using multiple specialized LLM agents working in parallel.

When a PR is opened or updated, the system picks up the event, splits the diff across four specialized review agents, and runs them concurrently. Each agent focuses on a different concern, and their findings are merged into a single, coherent review that gets posted back to the PR as inline comments.

One thing I wanted to get right: the system doesn't just review in isolation. It tracks recurring issues across the codebase over time, so the review agents get sharper and more context-aware the more the codebase is used. Effectively, known as 'self-improving' agent. 

Built as five independent FastAPI microservices with Celery workers, orchestrated by
[LangGraph](https://langchain-ai.github.io/langgraph/), running on AWS EKS provisioned
entirely through Terraform.

**Check the Video Demo Here** : [Live Demo](https://drive.google.com/file/d/1t3Gw89OCfneoaLq3DsmdPbNPxQinGkb-/view?usp=sharing)  
**Grafana Dashboard** : [Video](https://drive.google.com/file/d/1CRnoLamaE6kUY000NVZjDnrevGyyiE3q/view?usp=sharing)


```
GitHub PR opened  ──▶  4 AI agents review in parallel  ──▶  Inline comments on the PR
                                    ▲                                    │
                                    └────── learned team patterns ◀──────┘ (on merge)
```

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Request lifecycle](#request-lifecycle)
- [The review graph](#the-review-graph)
- [The learning loop](#the-learning-loop)
- [Data model](#data-model)
- [Repository layout](#repository-layout)
- [Tech stack](#tech-stack)
- [Getting started](#getting-started)
  - [1. Prerequisites](#1-prerequisites)
  - [2. Create the GitHub App](#2-create-the-github-app)
  - [3. Provision infrastructure](#3-provision-infrastructure)
  - [4. Configure secrets](#4-configure-secrets)
  - [5. Run database migrations](#5-run-database-migrations)
  - [6. Deploy the services](#6-deploy-the-services)
  - [7. Point the GitHub App at your cluster](#7-point-the-github-app-at-your-cluster)
- [CI/CD](#cicd)
- [Observability](#observability)
- [Quality evaluation](#quality-evaluation)
- [Configuration reference](#configuration-reference)
- [Operations runbook](#operations-runbook)
- [Cost and teardown](#cost-and-teardown)
- [Troubleshooting](#troubleshooting)

---

## What it does

When a pull request is opened, reopened, or updated:

1. **Verifies** the webhook came from GitHub using an HMAC SHA-256 signature.
2. **Records** the PR and returns `202 Accepted` immediately — no GitHub timeout.
3. **Fetches the diff** using a short-lived GitHub App installation token.
4. **Reviews the diff with four agents in parallel** — static analysis, security, style,
   and architecture.
5. **Deduplicates** findings and stores them in Postgres.
6. **Posts a review** to the PR with inline comments anchored to exact file and line.

When that pull request is **merged**, the system treats the findings as validated signal
and increments a per-repository pattern counter. The style agent reads the top patterns on
every future review, so the system adapts to your codebase.

---

## Architecture

Five services, each independently deployable and scalable. Solid lines are synchronous
HTTP; dashed lines are asynchronous Celery tasks over Redis.

### PR Review Flow

```mermaid
flowchart LR
    GH1[("GitHub<br/>PR opened/updated")] -->|"1 . webhook"| GW["gateway<br/>verify HMAC signature"]
    GW -->|"2"| WH["webhook<br/>filter + dedupe"]
    WH -->|"3 . enqueue"| RD[("Redis")]
    RD -->|"4"| WHW["webhook-worker"]
    WHW -->|"5 . analyze"| ORC["orchestrator<br/>LangGraph fan-out"]
    ORC -->|"6 . fetch diff"| GH1
    ORC -->|"7 . 4 parallel calls"| OAI["OpenAI<br/>gpt-4o-mini"]
    ORC -->|"8"| REV["reviewer"]
    REV -->|"9 . post inline comments"| GH1
    ORC -.->|"traces"| LF["Langfuse"]

    classDef svc fill:#dbeafe,stroke:#2563eb,color:#1e3a5f
    classDef store fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef ext fill:#fef3c7,stroke:#d97706,color:#78350f
    class GW,WH,WHW,ORC,REV svc
    class RD store
    class GH1,OAI,LF ext
```

### Learning Flow

```mermaid
flowchart LR
    GH2[("GitHub<br/>PR merged")] -->|"1 . webhook"| GW2["gateway"]
    GW2 -->|"2"| WH2["webhook"]
    WH2 -->|"3 . enqueue"| RD2[("Redis")]
    RD2 -->|"4"| LRNW["learner-worker"]
    LRNW -->|"5 . extract patterns"| LRN["learner"]
    LRN -->|"6 . store"| PG[("PostgreSQL<br/>patterns table")]

    classDef svc fill:#dbeafe,stroke:#2563eb,color:#1e3a5f
    classDef store fill:#dcfce7,stroke:#16a34a,color:#14532d
    classDef ext fill:#fef3c7,stroke:#d97706,color:#78350f
    class GW2,WH2,LRNW,LRN svc
    class RD2,PG store
    class GH2 ext
```

### Why it is split this way

| Service | Port | Responsibility | Why it is separate |
|---|---|---|---|
| `gateway` | 8000 | Verifies HMAC signature, forwards raw body | Only internet-facing pod; keeps the trust boundary tiny and stateless |
| `webhook` | 8001 | Filters actions, dedupes by `head_sha`, enqueues work | Must reply to GitHub in seconds; hands off slow work to a queue |
| `orchestrator` | 8002 | Runs the LangGraph agents, persists findings | The expensive, LLM-bound step — scales independently via HPA |
| `reviewer` | 8003 | Formats and posts the GitHub review | Isolates GitHub write-API rate limits and retry logic |
| `learner` | 8004 | Turns merged-PR findings into reusable patterns | Runs off the critical path, after the review is delivered |

The two Celery workers share images with `webhook` and `learner` but run a different
command, consuming the `webhook` and `learning` queues respectively.

---

## Request lifecycle

The full path of one pull request, from webhook to inline comment.

```mermaid
sequenceDiagram
    autonumber
    participant GH as GitHub
    participant GW as gateway
    participant WH as webhook
    participant Q as Redis queue
    participant W as webhook-worker
    participant OR as orchestrator
    participant AI as OpenAI
    participant DB as PostgreSQL
    participant RV as reviewer

    GH->>GW: POST /webhook/github (X-Hub-Signature-256)
    GW->>GW: hmac.compare_digest(expected, header)
    Note over GW: 401 immediately if signature fails
    GW->>WH: POST /events (raw body)

    WH->>WH: action in (opened, reopened, synchronize)?
    WH->>DB: SELECT by (repo, pr_number, head_sha)
    alt already seen this commit
        WH-->>GH: {"status": "already_processing"}
    else new commit
        WH->>DB: INSERT pull_requests (status='pending')
        WH->>Q: enqueue analyze_pr
        WH-->>GH: 202 Accepted
    end

    Q->>W: deliver analyze_pr
    W->>OR: POST /analyze

    OR->>GH: JWT (RS256) → installation access token
    OR->>GH: GET pull → diff (Accept: v3.diff)
    OR->>DB: SELECT top 10 patterns for repo

    par four agents at once
        OR->>AI: static analysis
    and
        OR->>AI: security (OWASP)
    and
        OR->>AI: style (+ learned patterns)
    and
        OR->>AI: architecture
    end

    OR->>OR: merge + dedupe findings
    OR->>DB: INSERT findings
    OR->>RV: POST /post-review

    RV->>GH: POST reviews (summary + inline comments)
    alt 422 — line not in diff
        RV->>GH: retry with summary only
    end
    RV->>DB: UPDATE status='reviewed'
```

Two details worth calling out:

- **Idempotency** is keyed on `(repo_full_name, pr_number, head_sha)`. Pushing three
  commits produces three reviews; GitHub redelivering the same event produces one.
- **Graceful degradation** — GitHub rejects inline comments on lines outside the diff with
  `422`. The reviewer catches that and re-posts the summary alone rather than losing the
  entire review.

---

## The review graph

The orchestrator builds a LangGraph `StateGraph` whose entry point is a conditional
fan-out. All four agents receive the same diff and run concurrently; their outputs
accumulate into one list via an `operator.add` reducer.

```mermaid
flowchart LR
    START(["diff + learned patterns"]) --> FO{{"fan_out<br/>Send() × 4"}}

    FO --> A["<b>static_analysis</b><br/>complexity<br/>unused variables<br/>naming"]
    FO --> B["<b>security</b><br/>OWASP Top 10<br/>hardcoded secrets<br/>SQL injection"]
    FO --> C["<b>style</b><br/>formatting · readability<br/><i>+ team patterns</i>"]
    FO --> D["<b>architecture</b><br/>separation of concerns<br/>error handling<br/>dependencies"]

    A --> M["<b>merge</b><br/>dedupe on<br/>(file, line, agent, message)"]
    B --> M
    C --> M
    D --> M

    M --> E(["findings[]"])

    classDef agent fill:#e0e7ff,stroke:#4f46e5,stroke-width:2px,color:#312e81
    classDef ctrl fill:#fce7f3,stroke:#db2777,stroke-width:2px,color:#831843
    class A,B,C,D agent
    class FO,M ctrl
```

Each agent is asked to return a JSON array where every item has `file`, `line`,
`severity` (`info` / `warning` / `error`), and `message`. Because LLMs like to wrap JSON
in Markdown fences, responses go through a tolerant parser that strips code fences and
**returns an empty list rather than raising** — one malformed agent response degrades that
agent's contribution instead of failing the whole review.

The `style` agent is the only one whose prompt is dynamic: it is injected with the top 10
most frequent patterns for that repository.

---

## The learning loop

This is what separates the project from a stateless "LLM reviews a diff" script. A merged
PR is an implicit signal that its findings described real, accepted-as-worth-fixing issues.

```mermaid
flowchart TB
    M["PR merged<br/><code>action=closed, merged=true</code>"] --> L["learner: select findings<br/>where severity in (warning, error)"]
    L --> U["<b>UPSERT into patterns</b><br/>ON CONFLICT (repo_full_name, pattern_text)<br/>DO UPDATE frequency = frequency + 1"]
    U --> P[("patterns table")]
    P --> Q["Next PR: top 10 by frequency"]
    Q --> S["Injected into style agent prompt"]
    S --> R["More repo-aware review"]
    R -.->|"reviewed → merged"| M

    classDef hl fill:#fef3c7,stroke:#d97706,stroke-width:2px,color:#78350f
    class U hl
```

The upsert is a single atomic `INSERT ... ON CONFLICT DO UPDATE`, so concurrent merges
across repositories increment counters safely without a read-modify-write race.

---

## Data model

Three tables, created by Alembic revision `0001_initial`. UUID primary keys are generated
database-side with `gen_random_uuid()` from the `pgcrypto` extension.

```mermaid
erDiagram
    PULL_REQUESTS ||--o{ FINDINGS : produces
    PULL_REQUESTS {
        uuid id PK "gen_random_uuid()"
        text repo_full_name "owner/name"
        int pr_number
        text head_sha "dedupe key"
        bigint installation_id "GitHub App install"
        text status "pending | reviewed"
        timestamptz created_at
    }
    FINDINGS {
        uuid id PK
        uuid pr_id FK
        text file
        int line
        text severity "info | warning | error"
        text message
        text agent "which agent found it"
        timestamptz created_at
    }
    PATTERNS {
        uuid id PK
        text repo_full_name "UQ with pattern_text"
        text pattern_text "UQ with repo_full_name"
        int frequency "incremented on merge"
        timestamptz updated_at
    }
```

`patterns` is deliberately not linked by a foreign key to `findings` — a pattern outlives
the individual finding that created it and is scoped only to a repository.

---

## Repository layout

```
.
├── services/                     # Five independently built microservices
│   ├── gateway/                  #   Signature verification, public entrypoint
│   │   ├── main.py
│   │   ├── models.py             #   Pydantic settings + SQLAlchemy models
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── webhook/                  #   Event filtering, dedupe, task enqueue
│   │   ├── main.py
│   │   └── worker.py             #   Celery: analyze_pr, trigger_learning
│   ├── orchestrator/             #   The LLM review engine
│   │   ├── main.py               #   GitHub App auth + diff fetch + persistence
│   │   └── graph.py              #   LangGraph fan-out/merge definition
│   ├── reviewer/main.py          #   Posts the GitHub review
│   └── learner/                  #   Pattern extraction from merged PRs
│       ├── main.py
│       └── worker.py
├── db/
│   ├── alembic.ini
│   └── migrations/versions/0001_initial.py
├── infra/
│   ├── terraform/                # VPC, EKS, RDS, ElastiCache, ECR, S3, IAM/OIDC
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   └── outputs.tf
│   └── k8s/                      # Deployments, Services, Ingress, HPA, Jobs
│       ├── gateway.yaml          #   + ClusterIP and LoadBalancer Services
│       ├── webhook.yaml
│       ├── webhook-worker.yaml   #   Celery worker, queue: webhook
│       ├── orchestrator.yaml
│       ├── reviewer.yaml
│       ├── learner.yaml
│       ├── learner-worker.yaml   #   Celery worker, queue: learning
│       ├── configmap.yaml        #   Non-secret config
│       ├── secret.yaml.example   #   Copy to secret.yaml (gitignored)
│       ├── ingress.yaml          #   AWS ALB ingress
│       ├── hpa.yaml              #   Orchestrator autoscaling 2–6
│       ├── migration-job.yaml    #   One-shot Alembic upgrade head
│       └── evaluate-job.yaml     #   Weekly Ragas quality gate
├── monitoring/
│   ├── prometheus.yml            # Scrape config for all five services
│   └── grafana-dashboard.json
├── scripts/
│   ├── evaluate.py               # Ragas faithfulness / answer relevancy
│   └── Dockerfile
└── .github/workflows/            # Per-service CI/CD + scheduled evaluation
    ├── service-ci.yml            #   Reusable: test → build → push → deploy
    ├── gateway.yml               #   Path-filtered trigger per service
    ├── webhook.yml
    ├── orchestrator.yml
    ├── reviewer.yml
    ├── learner.yml
    └── evaluate.yml
```

Note that every `Dockerfile` copies from the **repository root** (`COPY services/gateway/ .`),
so all images must be built with the repo root as the build context.

---

## Tech stack

| Layer | Choice |
|---|---|
| API framework | FastAPI + Uvicorn, Python 3.11 |
| LLM orchestration | LangGraph (parallel `Send` fan-out), OpenAI `gpt-4o-mini` |
| LLM observability | Langfuse (via `langfuse.openai` drop-in client) |
| Async tasks | Celery with Redis broker and result backend |
| Database | PostgreSQL 15 (RDS) via SQLAlchemy async + `asyncpg`, Alembic migrations |
| Cache / queue | Redis 7 (ElastiCache) |
| Auth | GitHub App: HMAC SHA-256 webhooks, RS256 JWT → installation tokens |
| Container orchestration | AWS EKS 1.32, managed node group of `m7i-flex.large` |
| IaC | Terraform (`terraform-aws-modules` for VPC and EKS) |
| Registry | Amazon ECR, one repository per service |
| Ingress | AWS Load Balancer Controller (ALB) |
| Metrics | Prometheus + Grafana via `prometheus-fastapi-instrumentator` |
| Quality gates | Ragas (faithfulness, answer relevancy) on a weekly schedule |
| CI/CD | GitHub Actions with OIDC — no long-lived AWS keys |

---

## Getting started

### 1. Prerequisites

| Tool | Purpose |
|---|---|
| AWS account + `aws` CLI | Provisioning and cluster access |
| `terraform` ≥ 1.5 | Infrastructure |
| `kubectl` | Deployments |
| `docker` | Building images |
| OpenAI API key | The review agents |
| Langfuse account (optional) | LLM tracing |





### 2. Create the GitHub App

I use a GitHub App (not a personal access token) so the reviewer authenticates as its own
identity rather than as me. Under **Settings → Developer settings → GitHub Apps → New
GitHub App**, I set:

- **Webhook URL** — a placeholder for now; I replace it in step 7.
- **Webhook secret** — a strong random string, saved for later.
- **Repository permissions** — `Pull requests: Read & write`, `Contents: Read-only`.
- **Subscribe to events** — `Pull request`.

Then I generate a **private key** (`.pem` download) and note the **App ID**. I install the
app on whichever repositories I want reviewed.

### 3. Provision infrastructure

```bash
cd infra/terraform
terraform init

terraform apply \
  -var="cluster_name=ai-code-reviewer" \
  -var='db_password=<your-strong-password>'
```

> **zsh note to self:** always wrap the password in **single** quotes. In double quotes,
> `!` triggers history expansion and you'll get stuck at a `dquote>` prompt — I learned
> this the hard way.

This gives me a VPC (two AZs, public subnets — I skipped a NAT gateway to keep costs down),
the EKS cluster and node group, RDS PostgreSQL, ElastiCache Redis, six ECR repositories, an
S3 reports bucket, and the IAM/OIDC role that lets GitHub Actions deploy without static
keys.

Before applying, I edit `variables.tf` and change `github_repo` and
`github_repo_immutable` to my own repository — otherwise the OIDC trust policy won't let my
Actions runs assume the CI role.

I collect the endpoints I'll need:

```bash
terraform output rds_endpoint
terraform output redis_endpoint
terraform output eks_cluster_endpoint
```

Then point `kubectl` at the new cluster:

```bash
aws eks update-kubeconfig --name ai-code-reviewer --region us-east-1
```

Finally, I install the AWS Load Balancer Controller so `ingress.yaml` can provision an ALB —
the node role already carries the IAM policy it needs, since Terraform creates it as
`<cluster_name>-lbc-policy`.

### 4. Configure secrets

`infra/k8s/secret.yaml` is gitignored, so I create it from the template:

```bash
cp infra/k8s/secret.yaml.example infra/k8s/secret.yaml
```

And fill in every value, using the RDS endpoint from the previous step:

```yaml
stringData:
  GITHUB_WEBHOOK_SECRET: "<the webhook secret from step 2>"
  GITHUB_APP_ID: "<your app id>"
  GITHUB_APP_PRIVATE_KEY: |
    -----BEGIN RSA PRIVATE KEY-----
    <contents of the .pem file>
    -----END RSA PRIVATE KEY-----
  OPENAI_API_KEY: "sk-..."
  LANGFUSE_PUBLIC_KEY: "pk-lf-..."
  LANGFUSE_SECRET_KEY: "sk-lf-..."
  DATABASE_URL: "postgresql+asyncpg://dbadmin:<password>@<rds_endpoint>/codereviewer"
```

I use SQLAlchemy's async engine everywhere, so `DATABASE_URL` has to keep the
`postgresql+asyncpg://` driver prefix. I also update `REDIS_URL` in `configmap.yaml` to my
ElastiCache endpoint, then apply both:

```bash
kubectl apply -f infra/k8s/configmap.yaml
kubectl apply -f infra/k8s/secret.yaml
```

### 5. Run database migrations

I ship migrations inside the `webhook` image and run them as a one-shot Job:

```bash
kubectl apply -f infra/k8s/migration-job.yaml
kubectl wait --for=condition=complete job/db-migrate --timeout=120s
kubectl logs job/db-migrate
```

### 6. Deploy the services

I replace the hardcoded ECR account ID in the manifests with my own, build and push the
five service images, then apply everything:

```bash
export ECR=<account-id>.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin $ECR

for svc in gateway webhook orchestrator reviewer learner; do
  docker build -t $ECR/$svc:latest -f services/$svc/Dockerfile .
  docker push $ECR/$svc:latest
done

kubectl apply -f infra/k8s/
```

And confirm everything's healthy:

```bash
kubectl get pods
kubectl get ingress gateway-ingress
```

### 7. Point the GitHub App at my cluster

I grab the ALB's public address:

```bash
kubectl get ingress gateway-ingress -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
```

Then set the GitHub App's webhook URL to `http://<that-hostname>/webhook/github`. I open a
test pull request — within a minute a review shows up with inline comments.

```bash
# I like to watch the whole pipeline as it happens
kubectl logs -l app=gateway -f
kubectl logs -l app=orchestrator -f
```

---

## CI/CD

I gave each service its own path-filtered workflow that delegates to a reusable
`service-ci.yml`. That way, editing `services/reviewer/**` rebuilds and redeploys **only**
the reviewer — nothing else gets touched.

```mermaid
flowchart LR
    P["Push to main<br/>services/&lt;svc&gt;/**"] --> T["<b>test</b><br/>pip install<br/>pytest if tests/ exists"]
    T --> B["<b>build-and-push</b><br/>OIDC → ECR<br/>tag: SHA + latest"]
    B --> D["<b>deploy</b><br/>kubectl set image<br/>deployment/&lt;svc&gt;"]

    PR["Pull request"] --> T2["<b>test only</b><br/>no deploy"]

    classDef job fill:#dbeafe,stroke:#2563eb,stroke-width:2px,color:#1e3a5f
    class T,B,D,T2 job
```

I tag images with both the commit SHA and `latest`, but deploy with the SHA tag — so every
rollout is traceable to an exact commit, and `kubectl rollout undo` actually means
something.

For auth, I use **GitHub OIDC**: the workflow exchanges a short-lived identity token for the
`github-actions-ai-reviewer` IAM role. No AWS access keys live anywhere in this repo.

Repository secrets I set up:

| Secret | Value |
|---|---|
| `AWS_ROLE_ARN` | ARN of the Terraform-created `github-actions-ai-reviewer` role |
| `AWS_ACCOUNT_ID` | My 12-digit AWS account ID, for the ECR registry host |
| `EKS_CLUSTER_NAME` | e.g. `ai-code-reviewer` |
| `DATABASE_URL` | Used by the scheduled evaluation job |

---

## Observability

Every FastAPI service exposes Prometheus metrics at `/metrics` via
`prometheus-fastapi-instrumentator`, and pods carry `prometheus.io/scrape` annotations for
discovery. `monitoring/prometheus.yml` has static scrape targets for local use, and I export
`monitoring/grafana-dashboard.json` to import straight into Grafana.

I think of this as two complementary layers:

- **Prometheus / Grafana** — request rates, latency percentiles, and error rates per
  service. This tells me *"is the system healthy?"*
- **Langfuse** — every agent call is traced with prompt, completion, token counts, and
  cost, since the orchestrator imports `OpenAI` from `langfuse.openai`. This tells me *"why
  did the review say that, and what did it cost?"*

I wired health probes for both workload types: HTTP `GET /health` for the API services, and
`celery inspect ping` as an exec probe for the Celery workers.

I only autoscale where the load actually is — `orchestrator-hpa` scales the LLM-bound
orchestrator from 2 to 6 replicas at 70% CPU utilization.

---

## Quality evaluation

LLM output quality can regress silently when I change a prompt or swap a model, so I set up
`.github/workflows/evaluate.yml` to run a **Ragas** evaluation every Monday at 09:00 UTC
(and on demand via `workflow_dispatch`).

The job builds `scripts/Dockerfile`, runs it as a Kubernetes Job in-cluster, and:

1. Pulls the 50 most recent findings joined to their pull requests.
2. Scores them on **faithfulness** and **answer relevancy**.
3. **Fails the workflow if mean faithfulness drops below 0.7.**

Logs always print and the Job always gets cleaned up, pass or fail. I can also trigger the
**Evaluate** workflow manually from the Actions tab whenever I want an immediate check.

---

## Configuration reference

I read all settings via `pydantic-settings` from the environment, sourced from the
ConfigMap and Secret.

| Variable | Source | Description |
|---|---|---|
| `DATABASE_URL` | Secret | Postgres DSN, must use `postgresql+asyncpg://` |
| `REDIS_URL` | ConfigMap | Celery broker and result backend |
| `GITHUB_WEBHOOK_SECRET` | Secret | Shared secret for HMAC SHA-256 verification |
| `GITHUB_APP_ID` | Secret | Issuer (`iss`) of the RS256 JWT |
| `GITHUB_APP_PRIVATE_KEY` | Secret | PEM key; literal `\n` sequences are normalized |
| `OPENAI_API_KEY` | Secret | Agent inference |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Secret | Trace ingestion |
| `LANGFUSE_HOST` | ConfigMap | e.g. `https://us.cloud.langfuse.com` |
| `WEBHOOK_SERVICE_URL` … `LEARNER_SERVICE_URL` | ConfigMap | In-cluster service addresses. I provide these for convenience, but the services currently hardcode these URLs — I have to change both if I rename a Service |

Terraform variables I expose:

| Variable | Default | Description |
|---|---|---|
| `region` | `us-east-1` | AWS region |
| `cluster_name` | *(required)* | Prefixes nearly every resource name |
| `db_password` | *(required, sensitive)* | RDS master password |
| `github_repo` | `Akhilesh0013/GitHub-PR-Code-Reviewer` | `owner/name` trusted by the OIDC role |
| `github_repo_immutable` | `Akhilesh0013@…/…@…` | Numeric-ID OIDC subject form |
| `environment` | `staging` | Applied as a tag to all resources |

---

## Cost and teardown

This stack runs real AWS infrastructure — an EKS control plane, two `m7i-flex.large` nodes,
RDS, ElastiCache, and an ALB. **It bills by the hour whether or not any PRs come in**, so I
tear it down whenever I'm done experimenting:

```bash
# I delete Kubernetes-managed load balancers first, or Terraform fails on the VPC
kubectl delete -f infra/k8s/

cd infra/terraform
terraform destroy \
  -var="cluster_name=ai-code-reviewer" \
  -var='db_password=<your-strong-password>'
```

Deleting the Kubernetes resources before `terraform destroy` matters: the ALB and
`LoadBalancer` Service are created by controllers *inside* the cluster, so Terraform doesn't
know about them and can't delete the VPC while they still hold ENIs.

I set `skip_final_snapshot = true` on RDS and `force_delete = true` on the ECR repositories,
so **destroy is irreversible and takes the data with it** — worth knowing before I run it.

---

## Troubleshooting

Things that tripped me up while building this, and how I fixed them:

| Symptom | Cause and fix |
|---|---|
| Shell stuck at `dquote>` | zsh history-expanded `!` inside double quotes. `Ctrl+C`, then single quotes: `-var='db_password=...'` |
| `401 Invalid signature` in gateway | `GITHUB_WEBHOOK_SECRET` differs from the GitHub App setting. The signature covers the exact raw body, so any proxy that rewrites the payload breaks it too |
| Webhook returns `already_processing` | Expected — that `head_sha` was already reviewed. I push a new commit to trigger a fresh review |
| Review posts a summary but no inline comments | GitHub returned `422` because a finding pointed at a line outside the diff; I made the reviewer fall back to summary-only on purpose |
| Findings list is empty | An agent returned unparseable JSON, which my parser converts to an empty list. I check the Langfuse trace for the raw completion |
| Pods stuck `CrashLoopBackOff` on startup | Almost always `DATABASE_URL` — I check the `postgresql+asyncpg://` prefix and that the RDS security group allows `5432` from `10.0.0.0/16` |
| Celery tasks queue but never run | Worker can't reach Redis. I confirm `REDIS_URL` in the ConfigMap matches `terraform output redis_endpoint` |
| Ingress has no address | The AWS Load Balancer Controller isn't installed or lacks IAM permissions |
| GitHub Actions can't assume the role | `github_repo` / `github_repo_immutable` in `variables.tf` still point at the original repository |
| `terraform destroy` hangs on the VPC | Kubernetes-created load balancers still exist. I run `kubectl delete -f infra/k8s/` first |

---

## Extending the system

Here's how I designed it to be extended:

- **Add a review agent** — add a prompt to `PROMPTS`, register a node in `build_graph()`,
  add a `Send(...)` in `fan_out`, and an edge into `merge`. Nothing else needs to change.
- **Change the model** — the model name lives in `make_node`, in
  `services/orchestrator/graph.py`.
- **Tune the quality gate** — I set the 0.7 faithfulness threshold in `scripts/evaluate.py`.
- **Weight learned patterns** — I already track `patterns.frequency`; the style prompt
  currently just takes the top 10 unweighted, which is the next thing I'd improve.