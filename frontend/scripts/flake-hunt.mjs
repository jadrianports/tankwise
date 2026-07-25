#!/usr/bin/env node
// flake-hunt.mjs -- repeatable failure-capturing harness for the
// RecentTripsSection intermittent test failure (and any other
// intermittent this project's full suite develops).
//
// Why this exists: the failure has been observed twice in real full-suite
// runs, both times as the first `npm test` after heavy filesystem
// activity (a production build, a Playwright capture run), and both
// times the verbatim assertion text was lost -- only the two failing
// test names survived. A static-reasoning fix (commit 3490287's global
// afterEach(cleanup) in src/test/setup.ts) did not resolve it, and
// roughly 17 further clean runs since then never reproduced it either.
// This script exists to run the suite repeatedly under a few different
// conditions and, the moment a run fails, capture and retain the
// verbatim vitest JSON reporter output (assertionResults[].
// failureMessages -- the only mechanism that preserves an intermittent
// failure's exact text) instead of letting it scroll off a terminal and
// vanish.
//
// Plain Node ESM, zero new dependencies -- uses only Node builtins and
// the already-installed vitest CLI.
//
// Usage (from frontend/):
//   node scripts/flake-hunt.mjs [--runs N] [--after-build] [--load N]
//                                [--seed N] [--shuffle]
//
// The three campaign conditions this project's flake-hunt uses:
//   node scripts/flake-hunt.mjs --runs 10
//   node scripts/flake-hunt.mjs --runs 10 --after-build
//   node scripts/flake-hunt.mjs --runs 10 --load 12
//
// Flags:
//   --runs N        Run the suite N times (default 10).
//   --after-build   Run `npm run build` immediately before each suite
//                   run, reproducing the one condition under which the
//                   flake was actually observed.
//   --load N        For the duration of each suite run, spawn N detached
//                   busy-loop child processes for CPU contention. Always
//                   killed afterwards, including on an aborted run.
//   --seed N        Pass --sequence.seed=N through to vitest (only takes
//                   effect together with --shuffle).
//   --shuffle       Pass --sequence.shuffle.files through to vitest, so
//                   the campaign can also cover file-order variation.

import { spawn } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync, readFileSync, existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(__dirname, '..');
const IS_WIN = process.platform === 'win32';

function parseArgs(argv) {
  const args = {
    runs: 10,
    afterBuild: false,
    load: 0,
    seed: undefined,
    shuffle: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--runs') args.runs = Number(argv[++i]);
    else if (arg === '--after-build') args.afterBuild = true;
    else if (arg === '--load') args.load = Number(argv[++i]);
    else if (arg === '--seed') args.seed = Number(argv[++i]);
    else if (arg === '--shuffle') args.shuffle = true;
    else {
      console.error(`Unknown flag: ${arg}`);
      process.exit(1);
    }
  }
  if (!Number.isInteger(args.runs) || args.runs < 1) {
    console.error('--runs must be a positive integer');
    process.exit(1);
  }
  if (!Number.isInteger(args.load) || args.load < 0) {
    console.error('--load must be a non-negative integer');
    process.exit(1);
  }
  return args;
}

/** Quote a single shell argument for cmd.exe / POSIX sh -- wraps in
 * double quotes whenever the argument contains whitespace or a quote
 * character, leaves simple tokens (flags, plain paths) unquoted. */
function quoteArg(arg) {
  if (/[\s"]/.test(arg)) {
    return `"${arg.replace(/"/g, '\\"')}"`;
  }
  return arg;
}

/** Spawn a command, collecting combined stdout+stderr while ALSO
 * teeing it live to this process's own stdout/stderr, so a long run is
 * still observable in real time and not just after the fact. Resolves
 * with { code, combinedOutput }.
 *
 * On Windows, npm/npx resolve to .cmd shims that cannot be spawned
 * without `shell: true` (Node refuses with EINVAL otherwise) -- but
 * passing shell:true together with a separate `args` array triggers
 * Node's DEP0190 warning (unescaped argument concatenation). So this
 * builds one fully-quoted command STRING up front and spawns that
 * single string with no separate args array, which avoids the warning
 * while still handling paths/flags containing spaces correctly. */
function runCommand(command, cmdArgs, { cwd }) {
  return new Promise((resolvePromise, rejectPromise) => {
    const fullCommand = [command, ...cmdArgs].map(quoteArg).join(' ');
    const child = IS_WIN
      ? spawn(fullCommand, { cwd, shell: true })
      : spawn(command, cmdArgs, { cwd });
    let combinedOutput = '';

    child.stdout.on('data', (chunk) => {
      combinedOutput += chunk.toString();
      process.stdout.write(chunk);
    });
    child.stderr.on('data', (chunk) => {
      combinedOutput += chunk.toString();
      process.stderr.write(chunk);
    });
    child.on('error', rejectPromise);
    child.on('close', (code) => resolvePromise({ code, combinedOutput }));
  });
}

/** Spawn N detached busy-loop children for CPU contention. Returns a
 * kill() function that terminates all of them -- callers MUST call this
 * in a finally block, and it is also wired to SIGINT, so an aborted
 * campaign never leaves spinners running. */
let activeLoadKill = () => {};

function startLoad(n) {
  if (n === 0) {
    activeLoadKill = () => {};
    return { kill: () => {} };
  }
  const children = [];
  for (let i = 0; i < n; i++) {
    const child = spawn(
      process.execPath,
      ['-e', 'while (true) { Math.sqrt(Math.random()); }'],
      { stdio: 'ignore', detached: !IS_WIN }
    );
    children.push(child);
  }
  let killed = false;
  const kill = () => {
    if (killed) return;
    killed = true;
    for (const child of children) {
      try {
        child.kill('SIGKILL');
      } catch {
        // Already dead -- fine.
      }
    }
  };
  activeLoadKill = kill;
  return { kill };
}

/** Read a vitest JSON reporter document and return the flattened list of
 * failing assertions across every test file: { file, fullName,
 * failureMessages }. */
function extractFailures(jsonReportPath) {
  const document = JSON.parse(readFileSync(jsonReportPath, 'utf-8'));
  const failures = [];
  for (const testResult of document.testResults ?? []) {
    for (const assertion of testResult.assertionResults ?? []) {
      if (assertion.status === 'failed') {
        failures.push({
          file: testResult.name,
          fullName: assertion.fullName,
          failureMessages: assertion.failureMessages ?? [],
        });
      }
    }
  }
  return failures;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const runTimestamp = new Date().toISOString().replace(/[:.]/g, '-');
  const artifactsDir = join(FRONTEND_ROOT, '.flake-hunt', runTimestamp);
  mkdirSync(artifactsDir, { recursive: true });

  console.log(
    `flake-hunt: ${args.runs} run(s), afterBuild=${args.afterBuild}, ` +
      `load=${args.load}, shuffle=${args.shuffle}${
        args.shuffle && args.seed !== undefined ? ` seed=${args.seed}` : ''
      }`
  );
  console.log(`artifacts: ${artifactsDir}`);

  let failureCount = 0;
  const failedRuns = [];

  for (let run = 1; run <= args.runs; run++) {
    const runLabel = String(run).padStart(3, '0');
    const jsonPath = join(artifactsDir, `run-${runLabel}.json`);
    const logPath = join(artifactsDir, `run-${runLabel}.log`);

    if (args.afterBuild) {
      console.log(`[run ${runLabel}] npm run build ...`);
      const buildResult = await runCommand(
        IS_WIN ? 'npm.cmd' : 'npm',
        ['run', 'build'],
        { cwd: FRONTEND_ROOT }
      );
      if (buildResult.code !== 0) {
        console.error(`[run ${runLabel}] build failed -- skipping this run's suite invocation`);
        continue;
      }
    }

    const load = startLoad(args.load);
    let vitestResult;
    try {
      const vitestArgs = ['vitest', 'run', '--reporter=json', `--outputFile=${jsonPath}`];
      if (args.shuffle) {
        vitestArgs.push('--sequence.shuffle.files');
        if (args.seed !== undefined) vitestArgs.push(`--sequence.seed=${args.seed}`);
      }
      console.log(`[run ${runLabel}] npx ${vitestArgs.join(' ')}`);
      vitestResult = await runCommand(IS_WIN ? 'npx.cmd' : 'npx', vitestArgs, {
        cwd: FRONTEND_ROOT,
      });
    } finally {
      load.kill();
    }

    writeFileSync(logPath, vitestResult.combinedOutput, 'utf-8');

    const passed = vitestResult.code === 0 && existsSync(jsonPath);
    if (passed) {
      console.log(`[run ${runLabel}] PASS`);
      rmSync(jsonPath, { force: true });
      rmSync(logPath, { force: true });
      continue;
    }

    failureCount++;
    failedRuns.push(runLabel);
    console.log(`[run ${runLabel}] FAIL -- artifacts retained at ${jsonPath} / ${logPath}`);

    if (existsSync(jsonPath)) {
      const failures = extractFailures(jsonPath);
      if (failures.length === 0) {
        console.log(`[run ${runLabel}] vitest exited non-zero but the JSON report shows no failing assertions -- see ${logPath}`);
      }
      for (const failure of failures) {
        console.log(`\n[run ${runLabel}] FAILED: ${failure.file} :: ${failure.fullName}`);
        for (const message of failure.failureMessages) {
          console.log(message);
        }
      }
    } else {
      console.log(`[run ${runLabel}] no JSON report was written -- see ${logPath} for raw output`);
    }
  }

  const rate = args.runs > 0 ? ((failureCount / args.runs) * 100).toFixed(1) : '0.0';
  console.log('\n--- flake-hunt summary ---');
  console.log(`runs: ${args.runs}`);
  console.log(`failures: ${failureCount} (${rate}%)`);
  if (failedRuns.length > 0) {
    console.log(`failed run ids: ${failedRuns.join(', ')}`);
  }
  console.log(`artifacts directory: ${artifactsDir}`);

  process.exitCode = failureCount > 0 ? 1 : 0;
}

process.on('SIGINT', () => {
  console.log('\nflake-hunt: interrupted -- killing any load children before exit');
  activeLoadKill();
  process.exit(130);
});

main();
