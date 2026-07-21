// Self-hosted fonts (no runtime Google Fonts CDN call) so the app stays fully
// offline-capable behind Nginx. Import only the weights the type scale uses:
// Inter 400 (body/label) + 600 (semibold), Space Grotesk 600 (headings/display).
import '@fontsource/inter/400.css';
import '@fontsource/inter/600.css';
import '@fontsource/space-grotesk/600.css';
