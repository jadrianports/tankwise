import { useEffect, useRef, useState } from 'react';
import Autocomplete from '@mui/material/Autocomplete';
import TextField from '@mui/material/TextField';
import Box from '@mui/material/Box';
import Typography from '@mui/material/Typography';
import { SearchBoxCore, SearchSession } from '@mapbox/search-js-core';
import type { SearchBoxSuggestion } from '@mapbox/search-js-core';

// A resolved suggestion (coords sent to POST /api/route, D-07) or a
// free-typed value that never resolved to a suggestion (falls back to the
// raw string -- the backend's LocationField is already polymorphic).
export interface ResolvedAddress {
  value: string;
  label: string;
}

interface AddressAutocompleteProps {
  label: string;
  token: string | null;
  displayValue: string;
  onResolve: (result: ResolvedAddress) => void;
  disabled?: boolean;
}

const DEBOUNCE_MS = 200;
const MIN_QUERY_LENGTH = 3;

// Wraps @mapbox/search-js-core's low-level SearchBoxCore/SearchSession
// (NOT the prebuilt <SearchBox> web component -- its own styling system
// would fight the locked MUI theme.js scale, per 09-RESEARCH.md's Standard
// Stack). Calls suggest() on input and retrieve() on selection, using the
// pk. token fetched from GET /api/config.
//
// One SearchSession instance PER FIELD (Pitfall 3): every mounted
// AddressAutocomplete owns its own SearchBoxCore + SearchSession pair in a
// component-local ref, never a shared module-level singleton -- so the
// Start field's session can never double-bill or corrupt proximity bias
// for the Finish field's session, and vice versa. The session is rebuilt
// whenever the token changes (e.g. once at boot when GET /api/config
// resolves) and torn down on unmount.
function AddressAutocomplete({ label, token, displayValue, onResolve, disabled }: AddressAutocompleteProps) {
  const [inputValue, setInputValue] = useState(displayValue);
  const [options, setOptions] = useState<SearchBoxSuggestion[]>([]);
  const [loading, setLoading] = useState(false);
  const sessionRef = useRef<SearchSession<
    Parameters<SearchBoxCore['suggest']>[1],
    SearchBoxSuggestion,
    Awaited<ReturnType<SearchBoxCore['suggest']>>,
    Awaited<ReturnType<SearchBoxCore['retrieve']>>
  > | null>(null);

  useEffect(() => {
    if (!token) {
      sessionRef.current = null;
      return;
    }
    const core = new SearchBoxCore({ accessToken: token, country: 'us' });
    sessionRef.current = new SearchSession(core, DEBOUNCE_MS);
    return () => {
      sessionRef.current = null;
    };
  }, [token]);

  // Keep the field's text in sync with external resets -- swap, geolocate,
  // a demo-trip chip, or a recent-trip click all set displayValue from a
  // sibling section without typing into this field directly.
  useEffect(() => {
    setInputValue(displayValue);
  }, [displayValue]);

  const handleInputChange = async (newInputValue: string) => {
    setInputValue(newInputValue);
    const session = sessionRef.current;
    if (!session || newInputValue.trim().length < MIN_QUERY_LENGTH) {
      setOptions([]);
      return;
    }
    setLoading(true);
    try {
      const result = await session.suggest(newInputValue);
      setOptions(result.suggestions ?? []);
    } catch {
      setOptions([]);
    } finally {
      setLoading(false);
    }
  };

  const resolveSuggestion = async (suggestion: SearchBoxSuggestion) => {
    const session = sessionRef.current;
    if (!session) return;
    try {
      const response = await session.retrieve(suggestion);
      const feature = response.features?.[0];
      if (!feature) return;
      const [lng, lat] = feature.geometry.coordinates;
      const resolvedLabel = feature.properties.full_address || feature.properties.name || suggestion.name;
      setInputValue(resolvedLabel);
      setOptions([]);
      onResolve({ value: `${lat},${lng}`, label: resolvedLabel });
    } catch {
      // Retrieval failed -- leave the typed text as-is; the blur handler
      // below falls back to sending it as a raw string (D-07).
    }
  };

  const handleBlur = () => {
    const trimmed = inputValue.trim();
    // A value that was never resolved to a suggestion (free-typed address
    // or a hand-entered "lat,lng") falls back to sending the raw string --
    // the backend's LocationField is already polymorphic, so this needs no
    // special handling beyond passing it straight through.
    if (trimmed && trimmed !== displayValue) {
      onResolve({ value: trimmed, label: trimmed });
    }
    setOptions([]);
  };

  return (
    <Autocomplete
      freeSolo
      fullWidth
      size="small"
      loading={loading}
      disabled={disabled}
      options={options}
      filterOptions={(x) => x}
      inputValue={inputValue}
      getOptionLabel={(option) => (typeof option === 'string' ? option : option.name)}
      isOptionEqualToValue={(option, value) =>
        typeof option !== 'string' && typeof value !== 'string' && option.mapbox_id === value.mapbox_id
      }
      onInputChange={(_event, newInputValue, reason) => {
        if (reason === 'input') {
          void handleInputChange(newInputValue);
        } else {
          setInputValue(newInputValue);
        }
      }}
      onChange={(_event, newValue) => {
        if (newValue && typeof newValue !== 'string') {
          void resolveSuggestion(newValue);
        }
      }}
      onBlur={handleBlur}
      renderOption={({ key: _key, ...optionProps }, option) => (
        <Box component="li" key={typeof option === 'string' ? option : option.mapbox_id} {...optionProps}>
          {typeof option === 'string' ? (
            option
          ) : (
            <Box>
              <Typography variant="body2">{option.name}</Typography>
              <Typography variant="body2" color="text.secondary">
                {option.place_formatted}
              </Typography>
            </Box>
          )}
        </Box>
      )}
      renderInput={(params) => (
        <TextField
          {...params}
          label={label}
          helperText={token ? 'Address or lat,lng' : 'Autocomplete unavailable — type an address or lat,lng'}
        />
      )}
    />
  );
}

export default AddressAutocomplete;
