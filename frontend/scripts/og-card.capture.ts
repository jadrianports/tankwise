import { test, expect } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// Renders og-card.html (a committed build input, not a test artifact) to
// frontend/public/og-card.png (LNCH-04, D-12). Regenerate with:
//   npm run capture --prefix frontend -- og-card
// Unlike showcase.capture.ts, this spec loads a LOCAL template file --
// no docker-compose stack, no live app, no baseURL -- it is the OG card's
// dedicated renderer, reusing the same Playwright harness (D-12's "one
// tool, one script, one dependency") rather than adding a second
// image-rendering dependency.

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const templateUrl = pathToFileURL(path.join(__dirname, 'og-card.html')).toString();

// The OG card has exactly one correct destination inside the built
// bundle -- frontend/public/, which `vite build` copies verbatim into
// frontend/dist/, served at the site root by WhiteNoise's WHITENOISE_ROOT
// (config/settings/base.py). Deliberately NOT routed through
// playwright.config.ts's CAPTURE_OUT_DIR: that constant exists so a
// later plan can re-point the *app* screenshots at a throwaway directory
// when re-run against the live host, which does not apply here.
const outputPath = path.join(__dirname, '..', 'public', 'og-card.png');

test.describe('OG card capture (committed launch asset)', () => {
  // Fixed 1200x630 output at scale 1 -- the Open Graph spec size that
  // LinkedIn/Slack/X all key their large-image treatment off, overriding
  // playwright.config.ts's 1280x800 @2x defaults tuned for the app
  // screenshots instead.
  test.use({
    viewport: { width: 1200, height: 630 },
    deviceScaleFactor: 1,
  });

  test('og-card.png -- 1200x630 branded social card', async ({ page }) => {
    await page.goto(templateUrl);

    // Wait for the product's real webfonts AND the map backdrop image to
    // finish loading before capturing -- never a fixed sleep. A card
    // captured mid-font-load ships with fallback glyphs and nobody
    // notices until it's already unfurling in someone's feed.
    // page.waitForFunction polls this async predicate (Playwright's
    // default 'raf' polling) until it resolves truthy, re-reading
    // document.fonts.ready and decoding the backdrop <div>'s own
    // CSS background-image (not an <img> tag, so a plain img.decode()
    // isn't available on the element itself) on every attempt.
    await page.waitForFunction(async () => {
      await document.fonts.ready;
      const backdrop = document.querySelector('.card__backdrop');
      if (!backdrop) return false;
      const match = getComputedStyle(backdrop).backgroundImage.match(/url\("?([^")]+)"?\)/);
      if (!match) return false;
      const probe = new Image();
      probe.src = match[1];
      try {
        await probe.decode();
        return true;
      } catch {
        return false;
      }
    });

    // Screenshot the card root element (not the viewport), so the
    // output's intrinsic dimensions are exactly the card's own 1200x630
    // regardless of the viewport set above.
    const buffer = await page.locator('.card').screenshot({ path: outputPath });

    // Same non-blank floor the other captures use.
    expect(buffer.byteLength).toBeGreaterThan(50_000);

    // Assert the written PNG's intrinsic dimensions are exactly 1200x630
    // by reading the IHDR chunk's width/height directly out of the PNG
    // header bytes. This exists because a deviceScaleFactor regression
    // above would silently emit a 2400x1260 image that still looks fine
    // locally but no longer matches the declared og:image:width /
    // og:image:height meta tags in index.html.
    expect(buffer.readUInt32BE(16)).toBe(1200);
    expect(buffer.readUInt32BE(20)).toBe(630);
  });
});
