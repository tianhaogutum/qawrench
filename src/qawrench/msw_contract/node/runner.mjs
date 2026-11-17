// Node.js runner bundled with `qawrench msw-contract`.
//
// The OpenAPI checks, reports and CLI are Python. This file only executes the
// project's real MSW handlers, which are JavaScript:
//
// * Supervisor (default mode, started once by Python): reads JSON lines on stdin,
//   resolves the project's own `msw` package from the handlers file, then from the
//   working directory, and forks one fresh worker process per explicit case. The
//   worker gets a per-case deadline, a 256 MB heap, Node's permission model
//   (read-only file access, no subprocesses, workers or native addons), a minimal
//   environment and an 8 KiB output limit. One JSON result per case goes to stdout.
// * Worker (`--worker`): imports the handlers module, builds a Fetch Request and
//   resolves it with MSW's `getResponse`, then reports status, headers and the
//   decoded, size-bounded body over the IPC channel.
//
// Use trusted local handler modules: process isolation and the network guards
// provide reproducible failure containment, not a security boundary.
import { fork } from 'node:child_process';
import { readFileSync, realpathSync, statSync } from 'node:fs';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { MIMEType } from 'node:util';
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import tls from 'node:tls';
import dgram from 'node:dgram';
import dns from 'node:dns';

const runnerPath = fileURLToPath(import.meta.url);
const MIN_NODE_MAJOR = 24;
const LOG_LIMIT = 8192;

// Decode only complete UTF-8 characters at the byte boundary.
function utf8Prefix(value, maxBytes) {
  return new TextDecoder().decode(Buffer.from(value).subarray(0, maxBytes), { stream: true });
}

if (process.argv[2] === '--worker') worker();
else supervise();

// ---------------------------------------------------------------------------
// Worker: one explicit case in a fresh process

function denyNetwork() { throw new Error('Network access is disabled: handlers must return local mock responses'); }

function installNetworkGuards() {
  // These guards prevent accidental dependencies on a real service. This is not a hostile-code sandbox.
  globalThis.fetch = async () => denyNetwork();
  globalThis.WebSocket = class { constructor() { denyNetwork(); } };
  http.request = http.get = https.request = https.get = denyNetwork;
  net.connect = net.createConnection = net.Socket.prototype.connect = denyNetwork;
  tls.connect = dgram.createSocket = denyNetwork;
  for (const key of Object.keys(dns)) if (/^(lookup|resolve|reverse)/.test(key) && typeof dns[key] === 'function') dns[key] = denyNetwork;
  for (const key of Object.keys(dns.promises)) if (/^(lookup|resolve|reverse)/.test(key)) dns.promises[key] = async () => denyNetwork();
  syncBuiltinESMExports();
}

async function execute({ mswUrl, handlersPath, exportName, example, maxBodyBytes, baseUrl }) {
  const { getResponse } = await import(mswUrl);
  installNetworkGuards();
  const module = await import(pathToFileURL(handlersPath).href);
  const handlers = module[exportName] ?? (exportName === 'handlers' ? module.default?.handlers ?? module.default : undefined);
  if (!Array.isArray(handlers) || handlers.length === 0 || !handlers.every((h) => typeof h?.run === 'function' && h.info?.method)) {
    throw new Error(`Export ${exportName} must be a non-empty array of MSW HTTP handlers`);
  }
  const request = new Request(example.url, { method: example.method, headers: example.headers, body: example.body });
  const response = await getResponse(handlers, request, { baseUrl: baseUrl ?? undefined });
  if (!response) return { error: { code: 'unhandled-request', message: 'No handler returned a mocked response for this example. Relative handler patterns require an explicit --base-url.' } };
  if (response.status === 302 && response.headers.get('x-msw-intention') === 'passthrough') {
    return { error: { code: 'passthrough', message: 'Handler requested passthrough; no real request was made' } };
  }
  if (response.type === 'error' || response.status === 0) return { error: { code: 'network-error', message: 'Handler returned Response.error()' } };
  if (!Number.isInteger(response.status) || response.status < 100 || response.status > 599) return { error: { code: 'invalid-status', message: `Handler returned invalid HTTP status ${response.status}; expected 100–599` } };
  if (example.method === 'HEAD') {
    void response.body?.cancel().catch(() => {});
    return { response: { status: response.status, headers: Object.fromEntries(response.headers), body: '', bytes: 0 } };
  }
  const chunks = [];
  let size = 0;
  if (response.body) {
    const reader = response.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array)) return { error: { code: 'body-stream', message: 'Response streams must yield Uint8Array chunks, as required by Fetch' } };
      size += value.byteLength;
      if (size > maxBodyBytes) {
        void reader.cancel().catch(() => {});
        return { error: { code: 'body-limit', message: `Response exceeded ${maxBodyBytes} bytes` } };
      }
      chunks.push(Buffer.from(value));
    }
  }
  const bytes = Buffer.concat(chunks);
  let media;
  try { media = new MIMEType(response.headers.get('content-type') ?? ''); } catch { /* media validation reports this later */ }
  let body;
  try {
    const isJson = /\/(?:[^/]+\+)?json$/.test(media?.essence ?? '');
    const isText = media?.type === 'text';
    const charset = isText ? media.params.get('charset') ?? 'utf-8' : 'utf-8';
    body = new TextDecoder(charset, { fatal: isJson || isText }).decode(bytes);
  } catch (error) { return { error: { code: 'body-encoding', message: `Cannot decode response body: ${error.message}` } }; }
  return { response: { status: response.status, headers: Object.fromEntries(response.headers), body, bytes: size } };
}

function worker() {
  process.once('message', async (message) => {
    // A never-settling promise alone does not keep Node alive. Keep the IPC handle
    // referenced until completion so the supervisor can consistently enforce the deadline.
    process.channel.ref();
    let result;
    try { result = await execute(message); }
    catch (error) { result = { error: { code: 'handler-error', message: error?.message ?? String(error) } }; }
    process.send(result, () => process.exit(0));
  });
}

// ---------------------------------------------------------------------------
// Supervisor: resolve msw once, then one bounded worker per case

const CONDITIONS = new Set(['node', 'import', 'module-sync', 'default']);

function exportTarget(target) {
  if (typeof target === 'string' || target === null) return target;
  if (Array.isArray(target)) {
    for (const item of target) { const found = exportTarget(item); if (found !== undefined) return found; }
    return undefined;
  }
  if (target && typeof target === 'object') {
    for (const [condition, value] of Object.entries(target)) {
      if (!CONDITIONS.has(condition)) continue;
      const found = exportTarget(value);
      if (found !== undefined) return found;
    }
  }
  return undefined;
}

function findPackageDir(start) {
  for (let dir = resolve(start); ; dir = dirname(dir)) {
    const manifest = join(dir, 'node_modules', 'msw', 'package.json');
    try { if (statSync(manifest).isFile()) return dirname(manifest); } catch { /* keep walking up */ }
    if (dirname(dir) === dir) return undefined;
  }
}

// Resolve `msw` the way an ESM `import 'msw'` in the handlers file would: walk up
// node_modules folders from the handlers file (then the working directory) and
// pick the package's "." export under the node/import conditions.
function resolveMsw(handlersPath, cwd) {
  const starts = [dirname(handlersPath), cwd];
  for (const start of starts) {
    const found = findPackageDir(start);
    if (!found) continue;
    const packageDir = realpathSync(found);
    const manifest = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8'));
    let exportsField = manifest.exports;
    if (exportsField && typeof exportsField === 'object' && !Array.isArray(exportsField) && Object.keys(exportsField).some((key) => key.startsWith('.'))) exportsField = exportsField['.'];
    let entry = exportsField === undefined ? manifest.main ?? 'index.js' : exportTarget(exportsField);
    entry = typeof entry === 'string' ? resolve(packageDir, entry) : undefined;
    if (!entry) {
      try { entry = createRequire(join(start, 'noop.js')).resolve('msw'); } catch { continue; }
    }
    return { url: pathToFileURL(entry).href, version: manifest.version, packageDir, modulesDir: dirname(packageDir) };
  }
  // Layouts without node_modules folders (for example Yarn Plug'n'Play).
  for (const start of starts) {
    try {
      const entry = createRequire(join(start, 'noop.js')).resolve('msw');
      return { url: pathToFileURL(entry).href, version: undefined, packageDir: dirname(entry), modulesDir: dirname(entry) };
    } catch { /* try the next base */ }
  }
  return undefined;
}

function readable(path) {
  try { const real = realpathSync(path); return real === path ? [path] : [path, real]; } catch { return [path]; }
}

function supervise() {
  let config;
  let cancelActive;
  const write = (message, done) => process.stdout.write(`${JSON.stringify(message)}\n`, done);
  const fatal = (code, message) => write({ type: 'fatal', code, message }, () => process.exit(3));
  const stop = () => { cancelActive?.(); process.exit(0); };
  process.once('SIGINT', stop);
  process.once('SIGTERM', stop);

  async function start({ handlersPath }) {
    const major = Number(process.versions.node.split('.')[0]);
    if (major < MIN_NODE_MAJOR) return fatal('node-version', `Node.js ${MIN_NODE_MAJOR} or newer is required to execute MSW handlers; found v${process.versions.node}`);
    let msw;
    try { msw = resolveMsw(handlersPath, process.cwd()); } catch (error) { return fatal('msw-not-found', `cannot read the msw package: ${error?.message ?? error}`); }
    if (!msw) return fatal('msw-not-found', `cannot resolve the 'msw' package from ${dirname(handlersPath)} or ${process.cwd()}; install msw in the project that owns the handlers (npm install --save-dev msw)`);
    let api;
    try { api = await import(msw.url); } catch (error) { return fatal('msw-import', `cannot import msw from ${msw.packageDir}: ${error?.message ?? error}`); }
    if (typeof api.getResponse !== 'function') return fatal('msw-unsupported', `msw ${msw.version ?? ''} at ${msw.packageDir} does not export getResponse; use MSW 2 or newer`);
    const readPaths = [...new Set([dirname(runnerPath), process.cwd(), msw.modulesDir].flatMap(readable))];
    config = { handlersPath, mswUrl: msw.url, readPaths };
    write({ type: 'ready', node: process.versions.node, msw: msw.version ?? null, mswPath: msw.packageDir });
  }

  function runCase({ exportName, example, maxBodyBytes, baseUrl, timeoutMs }) {
    return new Promise((resolveCase) => {
      const child = fork(runnerPath, ['--worker'], {
        cwd: process.cwd(),
        env: { PATH: process.env.PATH, TZ: 'UTC', LANG: 'C.UTF-8', NODE_ENV: 'test' },
        execArgv: ['--max-old-space-size=256', '--permission', ...config.readPaths.map((path) => `--allow-fs-read=${path}`)],
        stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
      });
      let result;
      let forcedError;
      let logs = '';
      let logBytes = 0;
      const kill = (error) => { forcedError ??= error; child.kill('SIGKILL'); };
      cancelActive = () => kill({ code: 'cancelled', message: 'Handler execution cancelled' });
      const timer = setTimeout(() => kill({ code: 'timeout', message: `Handler process exceeded ${timeoutMs} ms (including import and body capture)` }), timeoutMs);
      for (const stream of [child.stdout, child.stderr]) {
        stream.on('data', (data) => {
          logs += utf8Prefix(data, Math.max(0, LOG_LIMIT - logBytes));
          logBytes += data.byteLength;
          if (logBytes > LOG_LIMIT) kill({ code: 'log-limit', message: `Handler produced more than ${LOG_LIMIT} bytes of output` });
        });
      }
      child.on('message', (message) => { result = message; });
      child.once('error', (error) => { forcedError = { code: 'process-error', message: error.message }; });
      child.once('close', (code, exitSignal) => {
        clearTimeout(timer);
        cancelActive = undefined;
        resolveCase({ ...(forcedError ? { error: forcedError } : result ?? { error: { code: 'process-exit', message: `Handler process exited before returning a response (code ${code}, signal ${exitSignal})` } }), ...(logs ? { diagnostics: logs } : {}) });
      });
      child.send({ mswUrl: config.mswUrl, handlersPath: config.handlersPath, exportName, example, maxBodyBytes, baseUrl }, (error) => {
        if (error) kill({ code: 'process-error', message: error.message });
      });
    });
  }

  const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
  lines.on('line', (line) => {
    let message;
    try { message = JSON.parse(line); } catch { return; }
    if (message.type === 'start') {
      start(message).catch((error) => fatal('runner-error', error?.message ?? String(error)));
    } else if (message.type === 'case') {
      if (!config) write({ type: 'result', result: { error: { code: 'process-error', message: 'The Node runner was not started' } } });
      else runCase(message).then((result) => write({ type: 'result', result }));
    } else if (message.type === 'cancel') {
      cancelActive?.();
    }
  });
  // Python closed the channel: stop the active case; the process exits once it is reaped.
  lines.on('close', () => cancelActive?.());
}
