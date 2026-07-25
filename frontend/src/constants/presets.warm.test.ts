// Drift guard for .github/workflows/keep-warm.yml's `warm` job.
//
// Why this test exists: the warm job POSTs a hardcoded JSON body per
// demo route (GitHub Actions workflow YAML has no way to import
// presets.ts at runtime), so nothing keeps those bodies in sync with
// PRESET_ROUTES/VEHICLE_PRESETS except a committed test. A warmed body
// that diverges from the frontend's own request body -- a stale
// coordinate, a missing `vehicle` object, a wrong waypoints order --
// produces a DIFFERENT `route:v4:` cache key (routing/cache.py's
// build_cache_key) than the one a chip click actually reads. That warms
// a cache entry no user will ever see: a silent, invisible failure with
// no test failure anywhere else to catch it. This test is the only
// thing that catches it -- add or edit a demo chip and this test fails
// until keep-warm.yml is updated to match.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { describe, expect, test } from 'vitest';

import { PRESET_ROUTES, VEHICLE_PRESETS, HERO_VEHICLE_PRESET_ID } from './presets';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKFLOW_PATH = resolve(__dirname, '../../../.github/workflows/keep-warm.yml');

const heroPreset = VEHICLE_PRESETS.find((preset) => preset.id === HERO_VEHICLE_PRESET_ID);
if (!heroPreset) {
  throw new Error(`HERO_VEHICLE_PRESET_ID '${HERO_VEHICLE_PRESET_ID}' has no matching VEHICLE_PRESETS entry`);
}

const WARMABLE_ROUTES = PRESET_ROUTES.filter((route) => !route.excludeFromWarming);
const EXCLUDED_ROUTES = PRESET_ROUTES.filter((route) => route.excludeFromWarming);

/** Extract every `--data '<json>'` payload from the workflow file's curl
 * steps. A plain-text regex, not a YAML parser -- the workflow has no
 * other single-quoted `--data` occurrences, and the `ping` job (which
 * this test does not care about) makes no POST at all. */
function extractWarmBodies(workflowYaml: string): Record<string, unknown>[] {
  const dataFlagPattern = /--data\s+'([^']*)'/g;
  const bodies: Record<string, unknown>[] = [];
  let match: RegExpExecArray | null;
  while ((match = dataFlagPattern.exec(workflowYaml)) !== null) {
    bodies.push(JSON.parse(match[1]));
  }
  return bodies;
}

describe('keep-warm.yml warm job matches PRESET_ROUTES', () => {
  const workflowYaml = readFileSync(WORKFLOW_PATH, 'utf-8');
  const warmBodies = extractWarmBodies(workflowYaml);

  test('warms exactly one body per non-excluded preset route', () => {
    expect(warmBodies).toHaveLength(WARMABLE_ROUTES.length);
  });

  test('every warmable preset has a matching warm body with the correct start/finish', () => {
    for (const route of WARMABLE_ROUTES) {
      const match = warmBodies.find(
        (body) => body.start === route.start && body.finish === route.finish
      );
      expect(match, `no warm body found for ${route.label}`).toBeDefined();
    }
  });

  test('the multi-stop preset carries its waypoints array in the same order', () => {
    const multiStop = WARMABLE_ROUTES.find((route) => route.waypoints && route.waypoints.length > 0);
    expect(multiStop, 'expected at least one warmable multi-stop preset to test against').toBeDefined();
    const match = warmBodies.find(
      (body) => body.start === multiStop!.start && body.finish === multiStop!.finish
    );
    expect(match).toBeDefined();
    expect(match!.waypoints).toEqual(multiStop!.waypoints);
  });

  test('every warm body carries the hero vehicle object with the exact three numeric fields', () => {
    expect(warmBodies.length).toBeGreaterThan(0);
    for (const body of warmBodies) {
      expect(body.vehicle).toEqual({
        mpg: heroPreset!.vehicle.mpg,
        tank_range_mi: heroPreset!.vehicle.tank_range_mi,
        starting_fuel: heroPreset!.vehicle.starting_fuel,
      });
    }
  });

  test('an excludeFromWarming preset (e.g. the unroutable Catalina chip) appears in NO warm body', () => {
    expect(EXCLUDED_ROUTES.length).toBeGreaterThan(0);
    for (const excluded of EXCLUDED_ROUTES) {
      const match = warmBodies.find(
        (body) => body.start === excluded.start && body.finish === excluded.finish
      );
      expect(match, `${excluded.label} must never be warmed`).toBeUndefined();
    }
  });
});
