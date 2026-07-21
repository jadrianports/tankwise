import { useState, type FocusEvent } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import TextField from '@mui/material/TextField';
import LinkIcon from '@mui/icons-material/Link';
import DownloadIcon from '@mui/icons-material/Download';
import MapIcon from '@mui/icons-material/Map';

import { downloadStopsCsv } from './csvExport';
import { downloadTripGeoJson } from './geoJsonExport';
import type { RouteResponse } from '../../types/routeContract';

export interface ShareExportBarProps {
  data: RouteResponse | null;
  shareUrl: string | null;
}

// UX-04: copy-share-link + CSV + GeoJSON controls. Disabled until a plan
// exists (nothing to share/export before the first solve). Rendered by
// App.tsx directly -- print-hidden by the caller's own wrapper (D-30: this
// bar is chrome, not the printable route sheet's content).
function ShareExportBar({ data, shareUrl }: ShareExportBarProps) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'manual'>('idle');
  const disabled = !data || !shareUrl;

  const handleCopy = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopyState('copied');
      window.setTimeout(() => setCopyState('idle'), 2000);
    } catch {
      // Clipboard API unavailable (older browser, non-HTTPS origin) --
      // fall back to a read-only field the user can select/copy by hand.
      setCopyState('manual');
    }
  };

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, px: 3, py: 1.5 }}>
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button
          size="small"
          variant="outlined"
          startIcon={<LinkIcon />}
          disabled={disabled}
          onClick={handleCopy}
          sx={{ minHeight: 44 }}
        >
          {copyState === 'copied' ? 'Link copied' : 'Copy share link'}
        </Button>
        <Button
          size="small"
          variant="outlined"
          startIcon={<DownloadIcon />}
          disabled={disabled}
          onClick={() => data && downloadStopsCsv(data)}
          sx={{ minHeight: 44 }}
        >
          Export CSV
        </Button>
        <Button
          size="small"
          variant="outlined"
          startIcon={<MapIcon />}
          disabled={disabled}
          onClick={() => data && downloadTripGeoJson(data)}
          sx={{ minHeight: 44 }}
        >
          Export GeoJSON
        </Button>
      </Box>
      {copyState === 'manual' && shareUrl && (
        <TextField
          size="small"
          value={shareUrl}
          label="Copy this link manually"
          slotProps={{
            htmlInput: {
              readOnly: true,
              onFocus: (e: FocusEvent<HTMLInputElement>) => e.currentTarget.select(),
            },
          }}
          fullWidth
        />
      )}
    </Box>
  );
}

export default ShareExportBar;
