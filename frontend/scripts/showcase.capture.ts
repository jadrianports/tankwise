import { test, expect, type Page } from '@playwright/test';
import path from 'node:path';

import { CAPTURE_OUT_DIR } from '../playwright.config';

// Committed launch-asset capture pipeline (LNCH-01, D-19..D-22). Produces
// the five PNGs README.md's Screenshots section links to:
//   hero-light.png / hero-dark.png -- the hero demo chip's full-viewport
//     plan+map, both themes. The light shot doubles as the OG card's map
//     backdrop (a later plan), so it is framed map-forward, nothing
//     cropped.
//   results-panel.png -- the sidebar alone, both accordions expanded, so
//     the summary card, per-leg breakdown, and tank chart are all visible
//     in one shot.
//   multi-stop.png -- the multi-stop demo chip's full-viewport plan+map,
//     showing the lettered waypoint pin distinct from numbered fuel-stop
//     markers.
//   elevation-profile.png -- a clip of the elevation chart region, taken
//     against the multi-stop route (it crosses the Rockies, so the chart
//     shows real relief instead of a mostly-flat fallback).
// Regenerate the whole set with `npm run capture --prefix frontend --
// showcase` against a running `docker compose up -d --build` stack -- no
// manual editing step afterward. Every route below is one of the app's
// own demo chips, clicked by its accessible name exactly like a reviewer
// would, so the pictures can never disagree with what a reviewer clicks.
const MIN_MAP_SCREENSHOT_BYTES = 50_000;

const HERO_CHIP_LABEL = 'Los Angeles → New York City';
const MULTI_STOP_CHIP_LABEL = 'Los Angeles → Denver → Chicago';

const heroLightPath = path.join(CAPTURE_OUT_DIR, 'hero-light.png');
const heroDarkPath = path.join(CAPTURE_OUT_DIR, 'hero-dark.png');
const resultsPanelPath = path.join(CAPTURE_OUT_DIR, 'results-panel.png');
const multiStopPath = path.join(CAPTURE_OUT_DIR, 'multi-stop.png');
const elevationProfilePath = path.join(CAPTURE_OUT_DIR, 'elevation-profile.png');

// Every capture screenshots synchronously right after a readiness wait
// resolves; MUI's Accordion/Collapse and any other CSS transition could
// otherwise still be mid-animation at that exact instant. Killing
// transition/animation duration on the page Playwright drives keeps
// every capture deterministic -- it only affects what this script's own
// page sees, never the shipped app, and has no effect on Mapbox GL's own
// canvas-driven camera animation (a requestAnimationFrame loop, not a
// CSS transition), which the readiness wait below already accounts for.
async function preparePage(page: Page): Promise<void> {
  await page.goto('/');
  await page.addStyleTag({
    content: '*, *::before, *::after { transition-duration: 0s !important; animation-duration: 0s !important; }',
  });
}

// Shared by every capture below (not copy-pasted five times): clicks a
// demo chip by its accessible name -- the same interaction a reviewer
// performs -- then waits for two independent, observable signals rather
// than a fixed sleep: (1) the sidebar's "Total fuel cost" figure, proving
// the API answered, scoped to the desktop `complementary` landmark since
// the same text can also exist, hidden, in the mobile BottomSheet's DOM
// at this viewport; and (2) MapView's `__tankwiseMapReady` flag (plan
// 15-04's settled Candidate B), proving Mapbox actually finished drawing
// (idle) rather than mid-tile/terrain paint.
async function planDemoRoute(page: Page, chipLabel: string): Promise<void> {
  await page.getByRole('button', { name: chipLabel }).click();
  await expect(page.getByRole('complementary').getByText('Total fuel cost')).toBeVisible({ timeout: 90_000 });
  await page.waitForFunction(() => (window as Record<string, unknown>).__tankwiseMapReady === true);
}

// Reads the fuel-stop count out of the rendered DOM (never the network
// response, so the number printed is the number a reviewer actually
// sees) and prints one labelled line for the demo-trip stop-count
// reconciliation task to consume. The "Fuel stops" list (StopList.tsx)
// interleaves numbered fuel-stop rows (rendered as buttons, clickable to
// focus the map) with letter-badged user-waypoint rows (plain list
// items, not buttons) on a multi-stop trip -- counting only the button
// rows isolates the real fuel-stop count from the itinerary's total row
// count.
async function logMeasuredStopCount(page: Page, routeLabel: string): Promise<void> {
  const stopCount = await page.getByRole('list', { name: 'Fuel stops' }).getByRole('button').count();
  const totalCost = await page
    .getByRole('complementary')
    .getByText('Total fuel cost')
    .locator('xpath=following-sibling::*[1]')
    .innerText();
  console.log(`[measured stop count] ${routeLabel}: ${stopCount} fuel stops, total cost ${totalCost}`);
}

test.describe('v3 showcase capture (committed launch assets)', () => {
  test('hero-light.png -- hero route, light theme, full viewport', async ({ page }) => {
    await preparePage(page);
    await planDemoRoute(page, HERO_CHIP_LABEL);
    await logMeasuredStopCount(page, 'hero (coast-to-coast)');

    // Full viewport, not a clip -- the "what is this product" shot must
    // show the sidebar's solved plan AND the routed map together, and it
    // is also the OG card's backdrop, so the map needs to occupy enough
    // of the frame to survive being used as a full-bleed background.
    const buffer = await page.screenshot({ path: heroLightPath });

    // Same 50,000-byte floor smoke.capture.ts validated against a real
    // rendered capture in plan 15-04 -- guards the blank-WebGL-canvas
    // failure mode, not image quality.
    expect(buffer.byteLength).toBeGreaterThan(MIN_MAP_SCREENSHOT_BYTES);
  });

  test('hero-dark.png -- hero route, dark theme, full viewport', async ({ page }) => {
    await preparePage(page);

    // Flips the app's OWN dark-mode control (AppShell's useColorScheme
    // toggle button), not `page.emulateMedia` -- clicking it both swaps
    // MUI's palette AND, via useMapStyle's own
    // `setConfigProperty('basemap', 'lightPreset', 'night')` call, the
    // Mapbox Standard basemap's own lighting, so this is a genuinely dark
    // map, not a light UI under a dark browser hint. Toggled BEFORE
    // planning the route (rather than after) so the readiness wait below
    // observes the map's FIRST idle event already in dark mode, instead
    // of racing a leftover `true` flag left over from a prior solve --
    // MapView's readiness effect only resets on a `data` change, not on
    // the theme toggle itself, and no route has been planned yet here.
    await page.getByRole('button', { name: 'switch to dark mode' }).click();
    await planDemoRoute(page, HERO_CHIP_LABEL);

    const buffer = await page.screenshot({ path: heroDarkPath });

    expect(buffer.byteLength).toBeGreaterThan(MIN_MAP_SCREENSHOT_BYTES);
  });

  test('results-panel.png -- sidebar, both accordions expanded', async ({ page }) => {
    await preparePage(page);
    await planDemoRoute(page, HERO_CHIP_LABEL);

    await page.getByRole('button', { name: 'Per-leg breakdown' }).click();
    await page.getByRole('button', { name: 'Tank level along the route' }).click();
    // Both accordions animate open (CSS transitions are already killed by
    // preparePage, but the underlying React state update is still async)
    // -- wait for the leg-breakdown's own rendered content, never a fixed
    // sleep, before measuring the sidebar.
    await expect(page.getByText('driving ·')).toBeVisible();

    // The `<aside>` is a flexed, internally-scrolling panel
    // (`overflowY: auto`, height pinned to the viewport by the row
    // flex container's default `align-items: stretch`) -- with both
    // accordions expanded its content is taller than any reasonable
    // viewport. Rather than scroll it (which only ever exposes one
    // viewport-height slice at a time, guaranteeing a cropped shot),
    // opt it out of the flex stretch and let it grow to its natural
    // content height, then take a `fullPage` clip -- this is a
    // Playwright-script-only DOM tweak for the capture, never touching
    // shipped source.
    const aside = page.locator('aside');
    await aside.evaluate((el) => {
      el.style.overflowY = 'visible';
      el.style.height = 'auto';
      el.style.maxHeight = 'none';
      el.style.alignSelf = 'flex-start';
    });
    await page.evaluate(() => window.scrollTo(0, 0));

    const box = await aside.boundingBox();
    if (!box) throw new Error('Sidebar has no bounding box -- cannot clip a screenshot to it.');

    // No byte-floor assertion below: this is an element clip of the
    // sidebar with no WebGL canvas in frame, so the blank-canvas failure
    // mode the floor guards against cannot occur here.
    await page.screenshot({ path: resultsPanelPath, clip: box, fullPage: true });
  });

  test('multi-stop.png -- multi-stop route, default theme, full viewport', async ({ page }) => {
    await preparePage(page);
    await planDemoRoute(page, MULTI_STOP_CHIP_LABEL);
    await logMeasuredStopCount(page, 'multi-stop (Rockies crossing)');

    const buffer = await page.screenshot({ path: multiStopPath });

    expect(buffer.byteLength).toBeGreaterThan(MIN_MAP_SCREENSHOT_BYTES);
  });

  test('elevation-profile.png -- elevation chart region, multi-stop route', async ({ page }) => {
    await preparePage(page);
    // The multi-stop route crosses the Rockies, so its profile has real
    // relief instead of the hero route's likely mostly-flat fallback.
    await planDemoRoute(page, MULTI_STOP_CHIP_LABEL);

    // ElevationChart samples terrain asynchronously after the map itself
    // settles (idle) -- wait for one of its rendered stat chips, never a
    // fixed sleep, so this never captures the "Loading elevation
    // profile…" placeholder instead of the real chart.
    const chart = page.getByText('Elevation profile', { exact: true }).locator('xpath=..');
    await expect(chart.getByText(/ft climbed/)).toBeVisible({ timeout: 30_000 });
    await chart.scrollIntoViewIfNeeded();

    const box = await chart.boundingBox();
    if (!box) throw new Error('Elevation profile region has no bounding box -- cannot clip a screenshot to it.');

    // No byte-floor assertion: same reasoning as results-panel.png above.
    await page.screenshot({ path: elevationProfilePath, clip: box });
  });
});
