# Experimental findings: Chrome Built-in AI

This document records the Chrome inference observations that motivated the first `chrome-inference` implementation. It intentionally excludes unrelated machine-health or storage-cleanup work.

These are observations from one Windows 10 desktop Chrome installation on September 26, 2026. They are **not** a compatibility contract. Chrome can change model families, model files, API behavior, and hardware policy independently of this repository.

## Local model package observed

Chrome stored a large model component under:

```text
%LOCALAPPDATA%\Google\Chrome\User Data\OptGuideOnDeviceModel\2025.8.8.1141
```

The package contained:

| File | Observed size |
| --- | ---: |
| `weights.bin` | 4,269,932,544 bytes (~3.977 GiB) |
| `on_device_model_execution_config.pb` | 138 bytes |
| `manifest.json` | 247 bytes |
| `encoder_cache.bin` | 0 bytes |
| `adapter_cache.bin` | 0 bytes |

The package manifest reported:

```json
{
  "manifest_version": 2,
  "name": "Optimization Guide On Device Model",
  "version": "2025.8.8.1141",
  "BaseModelSpec": {
    "name": "v3Nano",
    "version": "2025.06.30.1229",
    "supported_performance_hints": [2, 1]
  }
}
```

The first bytes of `weights.bin` did not expose an obvious common model-container magic header. We therefore treat the file as a Chrome-private implementation detail and do not attempt to load it directly.

## Chrome internals observed

`chrome://on-device-internals` reported:

- performance class: `High`
- approximately 8008 MiB VRAM available, versus 3000 MiB required
- device capable: true
- image and audio listed as possible capabilities
- zero current model crashes
- model assets managed through Chrome's model manifest

The manifest advertised multiple model/component families, including `v3Nano`-era assets and newer Gemma 4 variants. Several newer components were not installed at the time of inspection.

This is why the framework targets `LanguageModel` and other Built-in AI capabilities rather than selecting a model file by name.

## API surface verified

The tested Chrome build exposed:

```javascript
typeof LanguageModel // "function"
typeof Summarizer    // "function"
```

`LanguageModel.availability()` initially returned `downloadable` in one call path and returned `available` once the requested language/configuration was explicit and Chrome had satisfied the model requirement.

A created language-model session exposed:

```text
contextWindow: 9216
contextUsage: 0 initially
append()
clone()
destroy()
measureContextUsage()
prompt()
promptStreaming()
oncontextoverflow
```

The old `inputQuota` and `inputUsage` properties were not present on this build.

## Context accounting

For the prompt:

```text
Explain how a Merkle tree works, with a small example.
```

`measureContextUsage()` returned 18 context units in the tested session.

Ten short prompt/response turns grew a fresh session as follows:

```text
0 -> 34 -> 71 -> 108 -> 149 -> 186 -> 225 -> 263 -> 301 -> 338 -> 381
```

This confirms that conversation history accumulates in session context. The framework uses Chrome's own `measureContextUsage()` rather than attempting to recreate its tokenizer/accounting externally.

## Streaming

`promptStreaming()` produced incremental text fragments through an async iterable / readable stream. The fragments were often token- or subword-sized (for example, `Mer`, `kle`, ` Trees`).

The localhost framework maps these fragments to Server-Sent Events rather than buffering the entire model response.

## Clone semantics

A base session was created with a system instruction and cloned into workers.

A state-isolation test then told clone A:

```text
Remember the secret word is ORANGE.
```

Subsequent results were:

```text
A: ORANGE.
B: I have no secret word.
```

The result is consistent with clone/fork semantics: a clone inherits context that existed at clone time, while later histories remain isolated.

The framework therefore caches small base/template sessions and clones them for individual requests.

## Concurrency experiment

Five classification requests were measured sequentially and then submitted concurrently across five clones.

Observed wall time:

```text
sequential: 2688.1 ms
parallel:   2899.1 ms
speedup:       0.93x
```

There was no throughput improvement from concurrent clones on this machine. This strongly suggests a shared inference bottleneck or scheduler for this workload.

The first framework implementation therefore serializes inference jobs. It does not equate logical session concurrency with physical model concurrency.

## Classification latency experiment

A base classifier session was cloned for 50 isolated classification jobs. Five simple statements were repeated ten times each.

Observed latency distribution:

| Metric | Time |
| --- | ---: |
| minimum | 392.8 ms |
| p50 | 613.0 ms |
| mean | 592.45 ms |
| p95 | 706.5 ms |
| maximum | 736.0 ms |

All 50 outputs matched the intended labels for those five simple repeated examples. This is a consistency check on a tiny synthetic set, **not** a meaningful accuracy benchmark.

## Repeatability experiment

The borderline statement:

```text
Demand will likely increase next quarter.
```

was classified 30 times from cloned baseline sessions.

Observed outputs:

| Label | Count | Share |
| --- | ---: | ---: |
| `INFERENCE` | 28 | 93.3% |
| `FACT` | 1 | 3.3% |
| `OPINION` | 1 | 3.3% |

This shows a strong modal answer but also demonstrates that prompt wording such as “deterministic classifier” does not make a generative model deterministic.

The framework's `/v1/classify` endpoint therefore uses Chrome's structured-output `responseConstraint` with an enum schema. This constrains output shape but does not turn semantic classification into an authoritative deterministic decision.

## Summarizer

`Summarizer.availability({ outputLanguage: "en" })` returned `available`, and `Summarizer.create()` successfully invoked Chrome's Built-in AI summarization path.

The framework exposes this separately instead of emulating summarization with a generic prompt.

## Design conclusions

The experiments support the following working model:

```text
multiple logical sessions / clones
              |
              v
    Chrome-managed inference lane
              |
              v
      browser-managed model
```

Useful properties:

- no separate model runtime is required
- model lifecycle is managed by Chrome
- context accounting is exposed directly
- streaming is available
- base sessions can be cloned cheaply for isolated tasks
- task-specific APIs such as `Summarizer` are available
- local inference has no per-call model API charge

Important constraints:

- the model remains generative and can vary across repeated calls
- logical clone concurrency did not improve throughput in this test
- Chrome controls model selection, downloads, retention, and upgrades
- APIs are document-oriented and currently unavailable in Web Workers
- model/file identities observed here must not be hardcoded as a compatibility dependency

## Next evaluation work

A useful next step is a frozen labeled corpus rather than additional toy prompts. At minimum, record:

- accuracy and confusion matrix
- per-class precision / recall
- repeated-trial disagreement rate
- warm and cold latency distributions
- context-use distribution
- schema-constrained output failures
- behavior across Chrome/model versions

That evaluation can determine whether Chrome Built-in AI is useful as a semantic fallback, annotator, extractor, router, or other auxiliary local provider for a given application.
