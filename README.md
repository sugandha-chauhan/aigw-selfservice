# AI Gateway Self-Service — portable bundle

Deploy a governed, self-service **Unity AI Gateway** endpoint-provisioning app into
**your own** Databricks workspace. Users pick their team + project; the app creates a
Model Serving endpoint with `cost_center` / `team` / `project` / `delete_after` tags and
AI Gateway (usage tracking + per-team rate limit). Spend then rolls up by tag in
`system.billing.usage`.

Ships as a **Databricks Asset Bundle (DAB)** — deploy with a couple of commands.

---

## What's in the box

```
aigw-selfservice-bundle/
├── databricks.yml            # bundle + variables + dev/prod targets
├── resources/
│   ├── app.yml               # the app + its SQL-warehouse binding
│   └── setup.yml             # one-time setup job (schema, table, views)
└── src/
    ├── app/                  # the Streamlit app (app.py, app.yaml, requirements, diagram)
    └── setup/setup_uc.py     # notebook that creates UC objects
```

The app has 6 tabs: **Provision · Use endpoint · Where are my tags · My endpoints · Cost by tag · Architecture**.

---

## Prerequisites

- Databricks CLI **≥ 0.239** (`databricks -v`), authenticated to your workspace
  (`databricks auth login --host https://<your-workspace>`)
- **Model Serving** enabled; a **SQL warehouse** (serverless is fine)
- Permission to create Apps, Jobs, and Unity Catalog schemas

---

## Setup (≈5 minutes)

### 1. Configure
Edit `databricks.yml` → your target: set `warehouse_id` (run `databricks warehouses list`
to find one), and optionally `catalog` / `schema`. Set the workspace `host` (or rely on
your CLI profile). App-side config (endpoint prefix, TTL days, secret names) lives in
`src/app/app.yaml`.

### 2. Create the proxy token secret
The app's example backend proxies Databricks Foundation Models via the
`databricks-model-serving` provider, which needs a workspace token:
```bash
databricks secrets create-scope aigw_selfservice
databricks secrets put-secret aigw_selfservice dbx_token --string-value "<a-databricks-token>"
```
> Provisioning/tagging/gateway/cost all work without a valid token; only **live inference**
> through the proxy needs it. (If PATs are disabled for you, use a short-lived
> `databricks auth token` value and refresh, or point the app at your own models.)

### 3. Deploy
```bash
databricks bundle deploy -t dev
databricks bundle run aigw_app -t dev      # starts the app + creates its service principal
```

### 4. Create the UC objects and grant the app's service principal
Get the app's service principal id, then run setup with it so grants are applied automatically:
```bash
SP=$(databricks apps get aigw-selfservice-dev -o json | jq -r '.service_principal_client_id')
databricks bundle deploy -t dev --var app_sp="$SP"
databricks bundle run setup_uc -t dev
# secret read for the SP (not SQL, so done here):
databricks secrets put-acl aigw_selfservice "$SP" READ
```

### 5. Open the app
```bash
databricks apps get aigw-selfservice-dev -o json | jq -r .url
```

---

## Customize for your org

- **Teams & cost centers:** edit `src/setup/setup_uc.py`'s seed rows (or just edit the
  `group_cost_center` table) so `user_group` values match your **Entra/SCIM group names**
  and their cost centers.
- **Models offered:** edit `AVAILABLE_MODELS` in `src/app/app.py`.
- **TTL / naming:** change `AIGW_TTL_DAYS`, `AIGW_ENDPOINT_PREFIX`, `AIGW_SCHEMA` in `src/app/app.yaml`.

---

## Design vs. demo (see the app's Architecture tab)

Two pieces are shown as **recommended production design**, not wired into the app:
1. **Entra/SCIM membership gate** — only allow provisioning for a team if the caller is a
   member of that team's Entra group (check via SCIM before the create call).
2. **Daily auto-delete job** — a scheduled Job that reads the `delete_after` tag and deletes
   endpoints past their 30-day TTL. (The app already *stamps* the tag; add the sweeper Job
   to enforce it.)

The runtime paths (provision, use, tag retrieval, cost rollup) are fully implemented.

---

## Teardown
```bash
databricks bundle destroy -t dev
# then remove any endpoints the app created (prefix from AIGW_ENDPOINT_PREFIX):
for e in $(databricks serving-endpoints list -o json | jq -r '.[].name | select(startswith("aigw-"))'); do
  databricks serving-endpoints delete "$e"; done
databricks secrets delete-scope aigw_selfservice
```
