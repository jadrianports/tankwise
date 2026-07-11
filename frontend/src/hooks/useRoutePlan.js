import { useCallback, useState } from 'react';

import { planRoute } from '../api/routeClient';

// Submit state machine: idle -> loading -> (success | error). Plain
// useState/useCallback is sufficient here -- no external query library needed
// for a single in-flight request per submit.
export function useRoutePlan() {
  const [status, setStatus] = useState('idle');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const submit = useCallback(async (start, finish) => {
    setStatus('loading');
    setError(null);

    const result = await planRoute(start, finish);

    if (result.ok) {
      setData(result.data);
      setStatus('success');
    } else {
      setData(null);
      setError({ code: result.code, message: result.message });
      setStatus('error');
    }

    return result;
  }, []);

  return { status, data, error, submit };
}
