# chrome-inference

`chrome-inference` is an experimental localhost provider for Chrome Built-in AI.

It lets a native process use the language model already managed by modern desktop Chrome without copying Chrome's model weights, reverse-engineering `weights.bin`, or installing a second LLM runtime.

The framework currently exposes:

- general prompting through `LanguageModel`
- streaming prompting through Server-Sent Events
- constrained classification using JSON Schema / `responseConstraint`
- context measurement and admission checks
- summarization through `Summarizer`
- model/capability health reporting
- reusable base sessions plus `clone()` for isolated jobs

## Architecture

Chrome's Built-in AI APIs are document APIs and are not currently available in Web Workers. Instead of trying to run inference in a daemon or directly loading Chrome's model files, this project keeps a small worker page open in Chrome:

```text
native CLI / application
        |
        | HTTP on 127.0.0.1
        v
chrome-inference broker (Python stdlib)
        |
        | queued job / result
        v
Chrome worker page
        |
        +--> LanguageModel
        +--> Summarizer
        |
        v
Chrome-managed local model runtime
```

The broker deliberately dispatches one inference job at a time. In the initial measurements documented in `docs/EXPERIMENTAL_FINDINGS.md`, five cloned sessions showed no throughput gain when submitted concurrently. `clone()` is therefore treated as a state-isolation and template-reuse primitive, not as evidence of parallel model execution.

## Requirements

- Python 3.10+
- a modern desktop Google Chrome build exposing the Built-in AI APIs
- hardware and storage that satisfy Chrome's own local-model requirements

Feature detection is always performed at runtime. The framework does not assume a particular Chrome model name or version.

Chrome documentation:

- https://developer.chrome.com/docs/ai/prompt-api
- https://developer.chrome.com/docs/ai/summarizer-api
- https://developer.chrome.com/docs/ai/get-started

## Install

From the repository:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Start the localhost broker:

```powershell
chrome-inference-server
```

The server creates a bearer token in:

```text
~/.chrome-inference/config.json
```

It binds only to loopback (`127.0.0.1` by default) and intentionally does not enable CORS.

Then open this URL in Chrome and keep the page open:

```text
http://127.0.0.1:8765/worker
```

The page shows Chrome's current `LanguageModel` and `Summarizer` availability. If either API reports `downloadable`, click **Initialize / download models**. Chrome requires a real user activation before a model download can begin.

## CLI

Inspect the current runtime:

```powershell
chrome-inference capabilities
```

Prompt the local model:

```powershell
chrome-inference prompt "Explain Merkle proofs in five sentences."
```

Use a reusable system template:

```powershell
chrome-inference prompt "The truck arrived at 14:03." `
  --system "Classify operational statements concisely."
```

Classification uses a response constraint rather than relying only on prompt wording:

```powershell
chrome-inference classify `
  "The carrier will probably be late because traffic is heavy." `
  --labels FACT INFERENCE OPINION
```

Summarize locally:

```powershell
chrome-inference summarize `
  "TCP establishes a reliable connection. UDP sends datagrams without delivery guarantees." `
  --type key-points `
  --length medium
```

Measure whether an input fits a session's context budget:

```powershell
chrome-inference measure "A long input to measure before inference"
```

## HTTP API

The broker exposes a small machine-local API. All `/v1/*` endpoints require:

```text
Authorization: Bearer <token from ~/.chrome-inference/config.json>
```

Endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | broker and worker connectivity |
| `GET` | `/v1/capabilities` | Chrome API availability and context window |
| `POST` | `/v1/prompt` | complete prompt response |
| `POST` | `/v1/prompt/stream` | SSE streaming prompt response |
| `POST` | `/v1/classify` | constrained enum classification |
| `POST` | `/v1/summarize` | Chrome Summarizer API |
| `POST` | `/v1/measure` | context usage estimate / fit check |

Example prompt body:

```json
{
  "prompt": "Explain why this shipment is at risk.",
  "system": "You are a concise logistics analyst.",
  "output_language": "en"
}
```

Example classification body:

```json
{
  "input": "Demand will likely increase next quarter.",
  "labels": ["FACT", "INFERENCE", "OPINION"],
  "instruction": "Classify the epistemic posture. Return only one allowed label.",
  "output_language": "en"
}
```

The prompt endpoint also accepts `response_schema`, which is passed to Chrome as `responseConstraint`.

## Why a worker page instead of an extension service worker?

Chrome's Prompt API is currently unavailable in Web Workers. A normal top-level localhost document is a supported execution context, gives Chrome a responsible document for permission-policy and user-activation checks, and keeps the bridge implementation small.

This also preserves a clean boundary: Chrome owns model downloads, GPU/CPU selection, model replacement, safety behavior, and inference execution. The project owns only request routing and application-level session policy.

## Session policy

The worker caches a small number of base sessions keyed by system prompt and language configuration. Each request clones the appropriate base session and destroys the clone afterward.

This follows the behavior observed experimentally and Chrome's own session-management guidance:

- clones inherit the parent context at clone time
- subsequent clone histories are isolated
- system/template context can be parsed once and reused
- unrelated jobs do not contaminate one another

## Model independence

Do not couple applications to Chrome's on-disk `weights.bin` path.

One inspected Windows installation used an `OptGuideOnDeviceModel` package whose manifest identified `v3Nano`, but Chrome's model manifest also advertised newer model families. Chrome may replace model assets independently of this project. The stable integration surface is the Built-in AI API, not a particular weight-file layout.

## Safety and reliability boundary

The local model is generative and should not be treated as a deterministic authority merely because it runs locally.

Our initial repeated classification test produced the modal label 28/30 times on one ambiguous statement. The framework therefore supports schema-constrained output, but callers should still use evaluation, validation, deterministic rules, or downstream gating for consequential decisions.

See `docs/EXPERIMENTAL_FINDINGS.md` for the exact measurements that motivated the current design.

## Development status

This is an experimental research/tooling project. The most important next work is:

1. run a real labeled evaluation corpus through the classifier endpoint
2. measure cold vs warm session latency
3. characterize context-overflow behavior through the broker
4. add cancellation propagation for disconnected streaming clients
5. test multiple Chrome versions and operating systems
6. evaluate whether a packaged extension/native bridge is worthwhile after the localhost-worker design is validated
