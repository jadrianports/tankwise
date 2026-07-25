import { test, expect } from '@playwright/test';

// Throwaway proof, not a launch asset: this is committed as the
// reproducible evidence that the capture harness (playwright.config.ts)
// actually produces a non-blank Mapbox GL screenshot, closing the one
// open question this phase's research left unresolved (a blank/black
// WebGL canvas screenshotting as if it were a real capture). Its output
// goes only to the gitignored test-results/ directory -- it never writes
// into docs/screenshots, which is reserved for the real launch-asset
// captures a later plan writes.
const MIN_MAP_SCREENSHOT_BYTES = 50_000;

test('a planned route renders a non-blank map that the harness can capture', async ({ page }) => {
  await page.goto('/');

  // The same interaction a reviewer performs -- click the demo chip by
  // its accessible name, not a direct API call.
  await page.getByRole('button', { name: 'Los Angeles → New York City' }).click();

  // The plan has rendered once the total-fuel-cost figure appears in the
  // results panel -- the observable signal that the API actually
  // answered, independent of anything map-related. A generous timeout
  // here (well past the default 5s) accommodates this app's own D-10
  // cold-start boot-retry, which can hold the "Waking up the server"
  // overlay open for up to ~60s on a just-started container or dyno.
  // Scoped to the desktop sidebar's `complementary` landmark -- the same
  // text is also present (but hidden) in the off-screen mobile
  // BottomSheet's DOM, which a bare page-wide text locator would
  // ambiguously match at this viewport.
  await expect(page.getByRole('complementary').getByText('Total fuel cost')).toBeVisible({ timeout: 90_000 });

  // Only once a plan exists does the map draw a route and fit its
  // camera to it -- wait for MapView's capture-only readiness flag
  // (set true on the map's own `idle` event) before screenshotting, so
  // this never races tile/terrain paint.
  await page.waitForFunction(() => (window as Record<string, unknown>).__tankwiseMapReady === true);

  // page.screenshot() composites the browser's own paint output, which
  // is the documented safe path for a WebGL canvas -- never call
  // canvas.toDataURL() or getImageData() from injected page script,
  // since a canvas readback (rather than a compositor screenshot) is the
  // exact call that can return a blank buffer.
  const mapPane = page.locator('main');
  const box = await mapPane.boundingBox();
  if (!box) throw new Error('Map pane has no bounding box -- cannot clip a screenshot to it.');

  const buffer = await page.screenshot({
    path: 'test-results/smoke-map.png',
    clip: box,
  });

  // A solid or near-solid region of this size PNG-compresses to a few
  // kilobytes, whereas a real rendered map with terrain, roads, labels,
  // and a route line does not. This is the automated stand-in for the
  // blank-WebGL-canvas failure mode -- a canvas whose drawing buffer was
  // cleared screenshots as a flat rectangle well under this floor.
  expect(buffer.byteLength).toBeGreaterThan(MIN_MAP_SCREENSHOT_BYTES);
});
