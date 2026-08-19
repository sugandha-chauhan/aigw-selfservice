"""
AI Gateway Self-Service Provisioning
------------------------------------
A governed, self-service front door for creating Model Serving endpoints.

Users pick their team + project; the app resolves the team's cost center and
rate limit from a Unity Catalog governance table, then provisions a Unity AI
Gateway-governed serving endpoint with cost_center / team / project tags
stamped on automatically. Spend then rolls up by tag in system.billing.usage.

Backend: external-model endpoints proxying Databricks Foundation Models via the
`databricks-model-serving` provider (scale-to-zero, near-zero idle cost).
"""

import os
import re
import json
import datetime as dt

import requests
import streamlit as st
from databricks.sdk.core import Config
from databricks import sql

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
# All config is env-driven (set in app.yaml) so the bundle is portable across
# workspaces/accounts without editing code.
CATALOG = os.getenv("AIGW_CATALOG", "main")
SCHEMA = os.getenv("AIGW_SCHEMA", "aigw_selfservice")
MAPPING_TABLE = f"{CATALOG}.{SCHEMA}.group_cost_center"
SECRET_SCOPE = os.getenv("AIGW_SECRET_SCOPE", "aigw_selfservice")
SECRET_KEY = os.getenv("AIGW_SECRET_KEY", "dbx_token")
ENDPOINT_PREFIX = os.getenv("AIGW_ENDPOINT_PREFIX", "aigw")   # endpoint name namespace
MANAGED_BY = "aigw-self-service"            # tag marking app-provisioned endpoints
TTL_DAYS = int(os.getenv("AIGW_TTL_DAYS", "30"))  # auto-delete tag horizon

AVAILABLE_MODELS = [
    "databricks-claude-opus-4-8",
    "databricks-gpt-5-6-sol",
    "databricks-gemini-3-6-flash",
    "databricks-meta-llama-3-3-70b-instruct",
    "databricks-claude-haiku-4-5",
]

cfg = Config()
WAREHOUSE_ID = os.getenv("DATABRICKS_WAREHOUSE_ID")

st.set_page_config(
    page_title="AI Gateway Self-Service",
    page_icon="🛡️",
    layout="wide",
)


# --------------------------------------------------------------------------- #
# Auth helpers
# --------------------------------------------------------------------------- #
def api_headers():
    h = dict(cfg.authenticate())
    h["Content-Type"] = "application/json"
    return h


def current_user() -> str:
    """Best-effort identity of the person using the app (set when deployed)."""
    try:
        headers = st.context.headers
        return (
            headers.get("x-forwarded-email")
            or headers.get("X-Forwarded-Email")
            or headers.get("x-forwarded-user")
            or "demo-user@databricks.com"
        )
    except Exception:
        return "demo-user@databricks.com"


@st.cache_resource
def _connection():
    return sql.connect(
        server_hostname=cfg.host.replace("https://", "").replace("http://", ""),
        http_path=f"/sql/1.0/warehouses/{WAREHOUSE_ID}",
        credentials_provider=lambda: cfg.authenticate,
    )


def run_sql(query: str):
    with _connection().cursor() as cur:
        cur.execute(query)
        cols = [c[0] for c in cur.description] if cur.description else []
        rows = cur.fetchall()
    return cols, [list(r) for r in rows]


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=60)
def load_groups():
    _, rows = run_sql(
        f"SELECT user_group, cost_center, rate_limit_per_min, default_model, description "
        f"FROM {MAPPING_TABLE} ORDER BY user_group"
    )
    return {
        r[0]: {
            "cost_center": r[1],
            "rate_limit_per_min": int(r[2]),
            "default_model": r[3],
            "description": r[4],
        }
        for r in rows
    }


def sanitize(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def build_payload(group, project, model, cost_center, rate_limit, user, enable_pii):
    ep_name = f"{ENDPOINT_PREFIX}-{sanitize(group)}-{sanitize(project)}"[:60].strip("-")
    ai_gateway = {
        "usage_tracking_config": {"enabled": True},
        "rate_limits": [
            {"calls": rate_limit, "renewal_period": "minute", "key": "endpoint"}
        ],
    }
    if enable_pii:
        ai_gateway["guardrails"] = {
            "input": {"pii": {"behavior": "BLOCK"}},
            "output": {"pii": {"behavior": "BLOCK"}},
        }
    payload = {
        "name": ep_name,
        "config": {
            "served_entities": [
                {
                    "name": "proxy",
                    "external_model": {
                        "name": model,
                        "provider": "databricks-model-serving",
                        "task": "llm/v1/chat",
                        "databricks_model_serving_config": {
                            "databricks_workspace_url": cfg.host,
                            "databricks_api_token": f"{{{{secrets/{SECRET_SCOPE}/{SECRET_KEY}}}}}",
                        },
                    },
                }
            ]
        },
        "ai_gateway": ai_gateway,
        "tags": [
            {"key": "cost_center", "value": cost_center},
            {"key": "team", "value": group},
            {"key": "project", "value": project},
            {"key": "requested_by", "value": user},
            {"key": "managed_by", "value": MANAGED_BY},
            {"key": "ttl_days", "value": str(TTL_DAYS)},
            {"key": "delete_after", "value": (dt.date.today() + dt.timedelta(days=TTL_DAYS)).isoformat()},
        ],
    }
    return ep_name, payload


def create_endpoint(payload):
    r = requests.post(
        f"{cfg.host}/api/2.0/serving-endpoints",
        headers=api_headers(),
        data=json.dumps(payload),
        timeout=60,
    )
    # Surface the result server-side so failures show up in `databricks apps logs`.
    print(f"[PROVISION] name={payload.get('name')} status={r.status_code} body={r.text[:800]}", flush=True)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"raw": r.text[:800]}


def grant_manage_to_user(endpoint_name: str, user: str):
    """Grant the requesting user CAN_MANAGE so the SP-created endpoint is
    visible/manageable to them in the Serving UI."""
    g = requests.get(
        f"{cfg.host}/api/2.0/serving-endpoints/{endpoint_name}",
        headers=api_headers(),
        timeout=30,
    )
    eid = g.json().get("id")
    if not eid:
        return None, "could not resolve endpoint id"
    r = requests.patch(
        f"{cfg.host}/api/2.0/permissions/serving-endpoints/{eid}",
        headers=api_headers(),
        data=json.dumps(
            {"access_control_list": [{"user_name": user, "permission_level": "CAN_MANAGE"}]}
        ),
        timeout=30,
    )
    print(f"[GRANT] endpoint={endpoint_name} user={user} status={r.status_code} body={r.text[:300]}", flush=True)
    return r.status_code, r.text[:300]


def list_managed_endpoints():
    r = requests.get(
        f"{cfg.host}/api/2.0/serving-endpoints", headers=api_headers(), timeout=60
    )
    out = []
    for e in r.json().get("endpoints", []):
        if e.get("name", "").startswith(ENDPOINT_PREFIX):
            tags = {t["key"]: t.get("value", "") for t in (e.get("tags") or [])}
            out.append(
                {
                    "name": e["name"],
                    "state": (e.get("state") or {}).get("ready", "—"),
                    "cost_center": tags.get("cost_center", "—"),
                    "team": tags.get("team", "—"),
                    "project": tags.get("project", "—"),
                    "requested_by": tags.get("requested_by", "—"),
                }
            )
    return out


def delete_endpoint(name):
    r = requests.delete(
        f"{cfg.host}/api/2.0/serving-endpoints/{name}",
        headers=api_headers(),
        timeout=60,
    )
    return r.status_code


def cost_by_tag():
    """Roll up MODEL_SERVING spend by governance tags.

    Reads a UC view (owner: demo user) over system.billing.usage, so the app
    service principal doesn't need direct access to the system table.
    """
    return run_sql(
        f"SELECT cost_center, team, dbus, records "
        f"FROM {CATALOG}.{SCHEMA}.cost_by_tag_v ORDER BY dbus DESC LIMIT 25"
    )


def managed_endpoint_names():
    try:
        return [e["name"] for e in list_managed_endpoints()]
    except Exception:
        return []


def query_endpoint(name: str, message: str, max_tokens: int = 200):
    """Send an OpenAI-compatible chat request to a provisioned endpoint."""
    r = requests.post(
        f"{cfg.host}/serving-endpoints/{name}/invocations",
        headers=api_headers(),
        data=json.dumps(
            {"messages": [{"role": "user", "content": message}], "max_tokens": max_tokens}
        ),
        timeout=60,
    )
    return r.status_code, r.json()


def get_tags_api(name: str):
    """Retrieval A — the endpoint object is the source of truth for tags."""
    r = requests.get(
        f"{cfg.host}/api/2.0/serving-endpoints/{name}", headers=api_headers(), timeout=30
    )
    d = r.json()
    return {t["key"]: t.get("value", "") for t in (d.get("tags") or [])}


def billing_for_endpoint(name: str):
    """Retrieval B — endpoint tags in system.billing.usage.custom_tags (owner view)."""
    return run_sql(
        f"SELECT endpoint_name, custom_tags, usage_unit, qty "
        f"FROM {CATALOG}.{SCHEMA}.endpoint_billing_v "
        f"WHERE endpoint_name = '{name}' ORDER BY qty DESC LIMIT 20"
    )


def usage_for_endpoint(name: str):
    """Retrieval C — per-request usage tracking (owner view over system.serving.*)."""
    return run_sql(
        f"SELECT request_time, requester, status_code, input_token_count, "
        f"output_token_count, usage_context FROM {CATALOG}.{SCHEMA}.endpoint_usage_v "
        f"WHERE endpoint_name = '{name}' ORDER BY request_time DESC LIMIT 20"
    )


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title("🛡️ AI Gateway Self-Service Provisioning")
st.caption(
    "Governed self-service: any team can stand up an AI Gateway endpoint in one "
    "click — cost center, team, and project tags are enforced automatically."
)

user = current_user()

try:
    groups = load_groups()
except Exception as e:
    st.error(
        "Could not read the governance mapping table "
        f"`{MAPPING_TABLE}`.\n\n**Check:** the app's service principal has "
        "`CAN USE` on the SQL warehouse and `SELECT` on the table.\n\n"
        f"Details: `{e}`"
    )
    st.stop()

tab_provision, tab_use, tab_tags, tab_manage, tab_cost, tab_arch = st.tabs(
    [
        "🚀 Provision endpoint",
        "🧪 Use endpoint",
        "🔎 Where are my tags",
        "📋 My endpoints",
        "💰 Cost by tag",
        "📐 Architecture",
    ]
)

# --- Provision -------------------------------------------------------------- #
with tab_provision:
    left, right = st.columns([1, 1], gap="large")

    with left:
        st.subheader("1 · Who & what")
        st.text_input("Requested by (auto-detected)", value=user, disabled=True)

        group = st.selectbox(
            "Team",
            options=list(groups.keys()),
            help="In production this is auto-detected from your SCIM group "
            "membership; here you can pick a persona to simulate each team.",
        )
        meta = groups[group]
        project = st.text_input(
            "Project name", placeholder="e.g. realtime-scoring", max_chars=40
        )
        model = st.selectbox(
            "Base model",
            options=AVAILABLE_MODELS,
            index=AVAILABLE_MODELS.index(meta["default_model"])
            if meta["default_model"] in AVAILABLE_MODELS
            else 0,
        )
        enable_pii = st.checkbox(
            "Enable PII guardrail (blocks PII in/out)", value=False,
            help="Unity AI Gateway guardrails (Public Preview).",
        )

    with right:
        st.subheader("2 · Governance applied automatically")
        st.metric("Cost center", meta["cost_center"])
        c1, c2 = st.columns(2)
        c1.metric("Rate limit", f"{meta['rate_limit_per_min']} / min")
        c2.metric("Auto-delete", f"{TTL_DAYS} days")
        st.info(f"**Team charter:** {meta['description']}")
        expires = (dt.date.today() + dt.timedelta(days=TTL_DAYS)).isoformat()
        st.markdown(
            "**Tags that will be enforced on the endpoint:**\n"
            f"- `cost_center` = `{meta['cost_center']}`\n"
            f"- `team` = `{group}`\n"
            f"- `project` = `{project or '…'}`\n"
            f"- `requested_by` = `{user}`\n"
            f"- `managed_by` = `{MANAGED_BY}`\n"
            f"- `ttl_days` = `{TTL_DAYS}` · `delete_after` = `{expires}`"
        )

    st.divider()
    disabled = not project.strip()
    if disabled:
        st.warning("Enter a project name to enable provisioning.")

    ep_name_preview, payload_preview = (
        build_payload(
            group, project or "project", model, meta["cost_center"],
            meta["rate_limit_per_min"], user, enable_pii,
        )
    )
    with st.expander("🔎 Preview the exact governed API call"):
        st.code(json.dumps(payload_preview, indent=2), language="json")

    if st.button(
        f"🚀 Provision `{ep_name_preview}`", type="primary", disabled=disabled
    ):
        with st.spinner("Creating Unity AI Gateway endpoint…"):
            ep_name, payload = build_payload(
                group, project, model, meta["cost_center"],
                meta["rate_limit_per_min"], user, enable_pii,
            )
            status, resp = create_endpoint(payload)
        if status in (200, 201):
            st.success(f"✅ Endpoint **{ep_name}** provisioned with governance tags.")
            # Share with the requesting user so it appears in their Serving UI
            # (SP-owned endpoints are otherwise invisible to non-admin users).
            gcode, gbody = grant_manage_to_user(ep_name, user)
            if gcode in (200, 204):
                st.caption(f"🔑 Shared with **{user}** (CAN_MANAGE) — it will now appear under Serving in the workspace.")
            else:
                st.caption(f"⚠️ Endpoint created, but sharing with you failed ({gcode}). It may only be visible to the app. Details: {gbody}")
            tags = {t["key"]: t["value"] for t in resp.get("tags", [])}
            st.json(
                {
                    "endpoint": ep_name,
                    "tags": tags,
                    "ai_gateway": resp.get("ai_gateway"),
                    "url": f"{cfg.host}/ml/endpoints/{ep_name}",
                }
            )
            st.balloons()
        elif status == 400 and resp.get("error_code") == "RESOURCE_ALREADY_EXISTS":
            st.warning(f"Endpoint **{ep_name}** already exists — sharing it with you.")
            gcode, gbody = grant_manage_to_user(ep_name, user)
            if gcode in (200, 204):
                st.caption(f"🔑 Shared with **{user}** (CAN_MANAGE) — refresh the Serving page and it will appear.")
            else:
                st.caption(f"⚠️ Could not share it ({gcode}): {gbody}")
        else:
            st.error(f"Provisioning failed (HTTP {status}).")
            st.json(resp)

# --- Use endpoint ----------------------------------------------------------- #
with tab_use:
    st.subheader("Call a provisioned endpoint")
    st.caption(
        "After provisioning, this is how a team actually consumes the endpoint — "
        "an OpenAI-compatible chat request through Unity AI Gateway."
    )
    names = managed_endpoint_names()
    if not names:
        st.info("No endpoints yet — provision one from the first tab.")
    else:
        ep = st.selectbox("Endpoint", names)
        msg = st.text_area("Message", value="Give me a one-sentence summary of Unity AI Gateway.")
        if st.button("Send request", type="primary"):
            with st.spinner("Calling endpoint through the gateway…"):
                code, resp = query_endpoint(ep, msg)
            if code == 200:
                try:
                    content = resp["choices"][0]["message"]["content"]
                except Exception:
                    content = json.dumps(resp)[:1200]
                st.success("200 · gateway recorded this call (usage tracking + rate limit applied)")
                st.markdown(f"> {content}")
            else:
                st.error(f"HTTP {code}")
                st.json(resp)
                if code == 401:
                    st.caption(
                        "401 = the proxy token in secret `schauhan_aigw_demo/dbx_token` "
                        "is missing/expired. Refresh it to enable live inference."
                    )
        with st.expander("📋 The equivalent call your teams would make"):
            st.code(
                f"curl -s $DATABRICKS_HOST/serving-endpoints/{ep if names else '<name>'}/invocations \\\n"
                f"  -H \"Authorization: Bearer $TOKEN\" -H \"Content-Type: application/json\" \\\n"
                f"  -d '{{\"messages\":[{{\"role\":\"user\",\"content\":\"Hello\"}}],\"max_tokens\":200}}'",
                language="bash",
            )

# --- Where are my tags ------------------------------------------------------ #
with tab_tags:
    st.subheader("Where the tags are stored — and how to retrieve them")
    st.caption(
        "The same cost_center / team / project tags surface in three real places. "
        "Everything below is a live query against this workspace."
    )
    names = managed_endpoint_names()
    if not names:
        st.info("No endpoints yet — provision one from the first tab.")
    else:
        ep = st.selectbox("Endpoint", names, key="tags-ep")

        st.markdown("##### A · The endpoint object — source of truth")
        st.caption("Serving API `GET /api/2.0/serving-endpoints/{name}` → `tags[]`. Real-time, authoritative.")
        try:
            st.json(get_tags_api(ep))
        except Exception as e:
            st.warning(f"Could not read endpoint tags: {e}")

        st.markdown("##### B · `system.billing.usage.custom_tags` — cost attribution")
        st.caption("Endpoint tags propagate here for chargeback; keyed by `usage_metadata.endpoint_name`. Billing lags ~hours.")
        try:
            cols, rows = billing_for_endpoint(ep)
            if rows:
                st.dataframe({c: [r[i] for r in rows] for i, c in enumerate(cols)}, use_container_width=True)
            else:
                st.info("No billing rows yet for this endpoint — expected for a freshly created / unused endpoint (hours of lag).")
        except Exception as e:
            st.warning(f"billing view error: {e}")

        st.markdown("##### C · `system.serving.*` — per-request usage tracking")
        st.caption("`endpoint_usage` ⋈ `served_entities` on `served_entity_id`. Carries requester / status / tokens / usage_context (not the endpoint tags — join those from A/B).")
        try:
            cols, rows = usage_for_endpoint(ep)
            if rows:
                st.dataframe({c: [str(r[i]) for r in rows] for i, c in enumerate(cols)}, use_container_width=True)
            else:
                st.info("No request rows yet — send a call from the 'Use endpoint' tab, then refresh (a few minutes of lag).")
        except Exception as e:
            st.warning(f"usage view error: {e}")

# --- Manage ----------------------------------------------------------------- #
with tab_manage:
    st.subheader("Endpoints provisioned through this app")
    if st.button("🔄 Refresh"):
        pass
    try:
        eps = list_managed_endpoints()
    except Exception as e:
        st.error(f"Could not list endpoints: {e}")
        eps = []
    if not eps:
        st.info("No self-service endpoints yet. Provision one from the first tab.")
    for e in eps:
        c = st.columns([3, 1, 1, 1, 2, 1])
        c[0].markdown(f"**{e['name']}**")
        c[1].markdown(f"`{e['cost_center']}`")
        c[2].markdown(f"`{e['team']}`")
        c[3].markdown(f"`{e['project']}`")
        c[4].caption(e["requested_by"])
        if c[5].button("🗑️", key=f"del-{e['name']}", help="Tear down"):
            code = delete_endpoint(e["name"])
            if code in (200, 204):
                st.success(f"Deleted {e['name']}")
                st.rerun()
            else:
                st.error(f"Delete failed (HTTP {code})")

# --- Cost ------------------------------------------------------------------- #
with tab_cost:
    st.subheader("MODEL_SERVING spend by governance tag — last 30 days")
    st.caption(
        "Reads `system.billing.usage`. Newly provisioned endpoints appear here "
        "once they accrue usage (billing data lags by hours)."
    )
    try:
        cols, rows = cost_by_tag()
        if rows:
            st.dataframe(
                {c: [r[i] for r in rows] for i, c in enumerate(cols)},
                use_container_width=True,
            )
        else:
            st.info("No MODEL_SERVING usage found in the window.")
    except Exception as e:
        st.warning(
            "Could not query `system.billing.usage` — the app's service "
            "principal likely needs `SELECT` on it (often admin-granted).\n\n"
            f"Details: `{e}`"
        )
        st.markdown("**The query the dashboard runs:**")
        st.code(
            "SELECT custom_tags['cost_center'] AS cost_center,\n"
            "       custom_tags['team']        AS team,\n"
            "       SUM(usage_quantity)        AS dbus\n"
            "FROM system.billing.usage\n"
            "WHERE billing_origin_product = 'MODEL_SERVING'\n"
            "  AND usage_unit = 'DBU'\n"
            "  AND usage_date >= current_date() - INTERVAL 30 DAYS\n"
            "GROUP BY 1, 2 ORDER BY dbus DESC",
            language="sql",
        )

# --- Architecture ----------------------------------------------------------- #
with tab_arch:
    st.subheader("How it works — flow, governance gate & auto-cleanup")
    st.caption(
        "Request → Entra-group check → use → where tags live → daily 30-day cleanup. "
        "Runtime paths are verified live; the membership gate and cleanup job are the "
        "recommended production design (not wired into this demo app)."
    )
    import streamlit.components.v1 as components

    diagram_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "diagram.html")
    try:
        with open(diagram_path, encoding="utf-8") as f:
            components.html(f.read(), height=2600, scrolling=True)
    except Exception as e:
        st.error(f"Could not load diagram.html: {e}")

st.divider()
st.caption(
    f"Governance source: `{MAPPING_TABLE}` · Backend: external-model proxies "
    f"(databricks-model-serving) · Endpoints namespaced `{ENDPOINT_PREFIX}-*`."
)
