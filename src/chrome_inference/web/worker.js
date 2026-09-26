const statusEl = document.querySelector('#status');
const logEl = document.querySelector('#log');
const initializeButton = document.querySelector('#initialize');
const refreshButton = document.querySelector('#refresh');
const runTestButton = document.querySelector('#run-test');
const promptEl = document.querySelector('#prompt');
const testOutputEl = document.querySelector('#test-output');

const baseSessions = new Map();
const MAX_BASE_SESSIONS = 8;
let capabilities = {};
let stopped = false;

function log(message, data) {
  const stamp = new Date().toLocaleTimeString();
  const suffix = data === undefined ? '' : ` ${JSON.stringify(data)}`;
  logEl.textContent = `[${stamp}] ${message}${suffix}\n${logEl.textContent}`.slice(0, 12000);
}

function languageModelOptions(language = 'en', inputLanguage = language) {
  const inputLanguages = [...new Set(['en', inputLanguage])];
  return {
    expectedInputs: [{ type: 'text', languages: inputLanguages }],
    expectedOutputs: [{ type: 'text', languages: [language] }],
  };
}

function summarizerOptions(payload) {
  const outputLanguage = payload.output_language || 'en';
  const inputLanguage = payload.input_language || outputLanguage;
  return {
    type: payload.type || 'key-points',
    format: payload.format || 'plain-text',
    length: payload.length || 'medium',
    expectedInputLanguages: [inputLanguage],
    outputLanguage,
    expectedContextLanguages: ['en'],
    sharedContext: payload.shared_context || undefined,
  };
}

async function detectCapabilities() {
  const next = {
    language_model_supported: typeof LanguageModel === 'function',
    summarizer_supported: typeof Summarizer === 'function',
    language_model_availability: 'unavailable',
    summarizer_availability: 'unavailable',
    context_window: null,
  };

  if (next.language_model_supported) {
    try {
      next.language_model_availability = await LanguageModel.availability(
        languageModelOptions('en')
      );
      if (next.language_model_availability === 'available') {
        const session = await LanguageModel.create(languageModelOptions('en'));
        next.context_window = session.contextWindow;
        session.destroy();
      }
    } catch (error) {
      next.language_model_error = String(error?.message || error);
    }
  }

  if (next.summarizer_supported) {
    try {
      next.summarizer_availability = await Summarizer.availability(
        summarizerOptions({ output_language: 'en' })
      );
    } catch (error) {
      next.summarizer_error = String(error?.message || error);
    }
  }

  capabilities = next;
  statusEl.textContent = JSON.stringify(next, null, 2);
  return next;
}

async function initializeModels() {
  if (!navigator.userActivation.isActive) {
    throw new Error('Chrome requires a user gesture to start a model download. Click the button directly.');
  }

  if (typeof LanguageModel === 'function') {
    const lmOptions = languageModelOptions('en');
    const availability = await LanguageModel.availability(lmOptions);
    log(`LanguageModel availability: ${availability}`);
    if (availability !== 'unavailable') {
      const session = await LanguageModel.create({
        ...lmOptions,
        monitor(monitor) {
          monitor.addEventListener('downloadprogress', event => {
            log(`LanguageModel download ${(event.loaded * 100).toFixed(1)}%`);
          });
        },
      });
      session.destroy();
    }
  }

  if (typeof Summarizer === 'function') {
    const options = summarizerOptions({ output_language: 'en' });
    const availability = await Summarizer.availability(options);
    log(`Summarizer availability: ${availability}`);
    if (availability !== 'unavailable') {
      const summarizer = await Summarizer.create({
        ...options,
        monitor(monitor) {
          monitor.addEventListener('downloadprogress', event => {
            log(`Summarizer download ${(event.loaded * 100).toFixed(1)}%`);
          });
        },
      });
      summarizer.destroy?.();
    }
  }

  await detectCapabilities();
  await heartbeat();
}

async function ensureLanguageModel(payload = {}) {
  if (typeof LanguageModel !== 'function') {
    throw new Error('LanguageModel API is not supported by this Chrome build.');
  }
  const outputLanguage = payload.output_language || 'en';
  const inputLanguage = payload.input_language || outputLanguage;
  const options = languageModelOptions(outputLanguage, inputLanguage);
  const availability = await LanguageModel.availability(options);
  if (availability !== 'available') {
    throw new Error(
      `LanguageModel is ${availability}. Open the worker page and click “Initialize / download models” if a download is required.`
    );
  }
  return options;
}

async function getBaseSession(system, payload = {}) {
  const options = await ensureLanguageModel(payload);
  const key = JSON.stringify({ system: system || '', options });
  const cached = baseSessions.get(key);
  if (cached) {
    return cached;
  }

  const createOptions = { ...options };
  if (system) {
    createOptions.initialPrompts = [{ role: 'system', content: system }];
  }
  const base = await LanguageModel.create(createOptions);
  baseSessions.set(key, base);

  if (baseSessions.size > MAX_BASE_SESSIONS) {
    const oldestKey = baseSessions.keys().next().value;
    const oldest = baseSessions.get(oldestKey);
    oldest?.destroy();
    baseSessions.delete(oldestKey);
  }
  return base;
}

async function runPrompt(job) {
  const payload = job.payload || {};
  const base = await getBaseSession(payload.system || '', payload);
  const session = await base.clone();
  const promptOptions = {};
  if (payload.response_schema) {
    promptOptions.responseConstraint = payload.response_schema;
  }

  try {
    if (job.stream) {
      const stream = session.promptStreaming(payload.prompt, promptOptions);
      for await (const chunk of stream) {
        await postEvent({ id: job.id, kind: 'chunk', text: chunk });
      }
      await postEvent({
        id: job.id,
        kind: 'done',
        result: {
          context_usage: session.contextUsage,
          context_window: session.contextWindow,
        },
      });
      return;
    }

    const text = await session.prompt(payload.prompt, promptOptions);
    return {
      text,
      context_usage: session.contextUsage,
      context_window: session.contextWindow,
    };
  } finally {
    session.destroy();
  }
}

async function runClassify(job) {
  const payload = job.payload || {};
  const labels = payload.labels;
  if (!Array.isArray(labels) || labels.length < 2) {
    throw new Error('classification requires at least two labels');
  }

  const instruction = payload.instruction ||
    `Classify the supplied statement. Return exactly one label from: ${labels.join(', ')}. Do not explain.`;
  const base = await getBaseSession(instruction, payload);
  const session = await base.clone();
  const schema = { type: 'string', enum: labels };

  try {
    const raw = await session.prompt(payload.input, { responseConstraint: schema });
    let label;
    try {
      label = JSON.parse(raw);
    } catch {
      label = raw.trim().replace(/^"|"$/g, '');
    }
    return {
      label,
      raw,
      context_usage: session.contextUsage,
      context_window: session.contextWindow,
    };
  } finally {
    session.destroy();
  }
}

async function runSummarize(job) {
  if (typeof Summarizer !== 'function') {
    throw new Error('Summarizer API is not supported by this Chrome build.');
  }
  const payload = job.payload || {};
  const options = summarizerOptions(payload);
  const availability = await Summarizer.availability(options);
  if (availability !== 'available') {
    throw new Error(
      `Summarizer is ${availability}. Open the worker page and click “Initialize / download models” if a download is required.`
    );
  }
  const summarizer = await Summarizer.create(options);
  try {
    const text = await summarizer.summarize(payload.text, {
      context: payload.context || undefined,
    });
    return { text };
  } finally {
    summarizer.destroy?.();
  }
}

async function runMeasure(job) {
  const payload = job.payload || {};
  const base = await getBaseSession(payload.system || '', payload);
  const session = await base.clone();
  const options = {};
  if (payload.response_schema) {
    options.responseConstraint = payload.response_schema;
  }
  try {
    const incoming = await session.measureContextUsage(payload.input, options);
    return {
      incoming_context_usage: incoming,
      current_context_usage: session.contextUsage,
      context_window: session.contextWindow,
      fits_with_reserve: session.contextUsage + incoming + (payload.reserve || 512) <= session.contextWindow,
    };
  } finally {
    session.destroy();
  }
}

async function execute(job) {
  switch (job.op) {
    case 'prompt':
      return runPrompt(job);
    case 'classify':
      return runClassify(job);
    case 'summarize':
      return runSummarize(job);
    case 'measure':
      return runMeasure(job);
    case 'capabilities':
      return detectCapabilities();
    default:
      throw new Error(`Unknown operation: ${job.op}`);
  }
}

async function postEvent(event) {
  const response = await fetch('/internal/event', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(event),
  });
  if (!response.ok) {
    throw new Error(`Broker event POST failed: ${response.status}`);
  }
}

async function heartbeat() {
  try {
    await fetch('/internal/heartbeat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ capabilities }),
    });
  } catch (error) {
    log('Heartbeat failed', String(error?.message || error));
  }
}

async function workerLoop() {
  log('Worker loop started. Inference jobs are intentionally serialized.');
  while (!stopped) {
    try {
      const response = await fetch('/internal/next', { cache: 'no-store' });
      if (!response.ok) {
        throw new Error(`next-job request failed: ${response.status}`);
      }
      const { job } = await response.json();
      if (!job) {
        continue;
      }
      log(`Running ${job.op}`, { id: job.id, stream: job.stream });
      try {
        const result = await execute(job);
        if (!job.stream) {
          await postEvent({ id: job.id, kind: 'result', result });
        }
        log(`Completed ${job.op}`, { id: job.id });
      } catch (error) {
        await postEvent({
          id: job.id,
          kind: 'error',
          error: String(error?.message || error),
        });
        log(`Failed ${job.op}`, { id: job.id, error: String(error?.message || error) });
      }
    } catch (error) {
      log('Worker loop error', String(error?.message || error));
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }
}

initializeButton.addEventListener('click', async () => {
  initializeButton.disabled = true;
  try {
    await initializeModels();
    log('Model initialization complete.');
  } catch (error) {
    log('Model initialization failed', String(error?.message || error));
  } finally {
    initializeButton.disabled = false;
  }
});

refreshButton.addEventListener('click', async () => {
  await detectCapabilities();
  await heartbeat();
});

runTestButton.addEventListener('click', async () => {
  runTestButton.disabled = true;
  testOutputEl.textContent = 'Running locally…';
  try {
    const pseudoJob = {
      id: 'direct-test',
      op: 'prompt',
      stream: false,
      payload: { prompt: promptEl.value, output_language: 'en' },
    };
    const result = await runPrompt(pseudoJob);
    testOutputEl.textContent = result.text;
  } catch (error) {
    testOutputEl.textContent = String(error?.message || error);
  } finally {
    runTestButton.disabled = false;
  }
});

window.addEventListener('beforeunload', () => {
  stopped = true;
  for (const session of baseSessions.values()) {
    session.destroy();
  }
});

(async () => {
  await detectCapabilities();
  await heartbeat();
  setInterval(heartbeat, 10000);
  workerLoop();
})();
