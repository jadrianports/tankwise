# Frontend

React + Material UI single-page app for the Fuel Route Optimizer map page. The root [README](../README.md) is the full project reference (architecture, API contract, Docker deployment) — this file only covers running the frontend on its own during local development.

## Local dev loop

```bash
npm install
npm run dev
```

`npm run dev` starts the Vite dev server (defaults to `http://localhost:5173`). Its dev proxy forwards any relative `/api/*` request to `http://localhost:8000`, so the SPA code never has to know whether it's talking to the Django dev server or, in production, Nginx. Run the API alongside it:

```bash
# from the repo root, in a separate terminal
python manage.py runserver
```

Make sure `MAPBOX_TOKEN` is set in the root `.env` before starting the Django dev server, or `/api/route` calls will 502.

## Other scripts

```bash
npm run build    # production bundle -> dist/
npm run lint     # oxlint
```

In production, `npm run build`'s output is what the Docker/Nginx stack (see the root README's Quickstart and Architecture sections) actually serves — Nginx serves the built `dist/` bundle and reverse-proxies `/api/*` to gunicorn on the same origin.
