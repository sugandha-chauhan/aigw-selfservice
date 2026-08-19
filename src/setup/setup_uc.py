# Databricks notebook source
# MAGIC %md
# MAGIC # AI Gateway Self-Service — one-time setup
# MAGIC Creates the governance schema, the `group_cost_center` mapping table, and the
# MAGIC owner-privilege views the app reads. Idempotent — safe to re-run.

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "aigw_selfservice")
dbutils.widgets.text("app_sp", "")  # optional: app service principal id to auto-grant

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
app_sp = dbutils.widgets.get("app_sp").strip()
fq = f"{catalog}.{schema}"
print(f"Setting up {fq}")

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {fq} COMMENT 'AI Gateway self-service governance'")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {fq}.group_cost_center (
  user_group STRING,
  cost_center STRING,
  rate_limit_per_min INT,
  default_model STRING,
  description STRING
)
""")

# Seed example rows only if empty — EDIT these to match your Entra groups + cost centers.
if spark.table(f"{fq}.group_cost_center").count() == 0:
    spark.sql(f"""
    INSERT INTO {fq}.group_cost_center VALUES
      ('ml-fraud','CC-4471',100,'databricks-claude-sonnet-4-5','Fraud ML team – real-time scoring'),
      ('genai-search','CC-8820',200,'databricks-gpt-oss-120b','GenAI search & RAG team'),
      ('data-science-rnd','CC-2093',50,'databricks-meta-llama-3-3-70b-instruct','Data science R&D / experimentation')
    """)
    print("Seeded 3 example groups — edit group_cost_center to match your org.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Owner-privilege views over system tables
# MAGIC These let the app's service principal read cost/usage without direct grants on
# MAGIC `system.*`. Requires the *person running this setup* to have access to the system
# MAGIC tables; wrapped so setup still succeeds if they don't.

# COMMAND ----------

views = {
  "cost_by_tag_v": f"""
    CREATE OR REPLACE VIEW {fq}.cost_by_tag_v AS
    SELECT COALESCE(custom_tags['cost_center'],'(untagged)') AS cost_center,
           COALESCE(custom_tags['team'], custom_tags['Team'],'(untagged)') AS team,
           ROUND(SUM(usage_quantity),2) AS dbus, COUNT(*) AS records
    FROM system.billing.usage
    WHERE billing_origin_product='MODEL_SERVING' AND usage_unit='DBU'
      AND usage_date >= current_date() - INTERVAL 30 DAYS
    GROUP BY 1,2""",
  "endpoint_billing_v": f"""
    CREATE OR REPLACE VIEW {fq}.endpoint_billing_v AS
    SELECT usage_metadata.endpoint_name AS endpoint_name, custom_tags, usage_unit,
           ROUND(SUM(usage_quantity),2) AS qty
    FROM system.billing.usage
    WHERE billing_origin_product='MODEL_SERVING'
      AND usage_date >= current_date() - INTERVAL 30 DAYS
    GROUP BY 1,2,3""",
  "endpoint_usage_v": f"""
    CREATE OR REPLACE VIEW {fq}.endpoint_usage_v AS
    SELECT e.endpoint_name, u.request_time, u.requester, u.status_code,
           u.input_token_count, u.output_token_count, u.usage_context
    FROM system.serving.endpoint_usage u
    JOIN system.serving.served_entities e USING (served_entity_id)""",
}

for name, ddl in views.items():
    try:
        spark.sql(ddl)
        print(f"created view {name}")
    except Exception as ex:
        print(f"WARNING: could not create {name} (need access to the underlying system table): {ex}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Grant the app service principal read access (optional)
# MAGIC Pass `app_sp` = the app's service principal id (from `databricks apps get <app>`).

# COMMAND ----------

if app_sp:
    grants = [
        f"GRANT USE SCHEMA ON SCHEMA {fq} TO `{app_sp}`",
        f"GRANT SELECT ON TABLE {fq}.group_cost_center TO `{app_sp}`",
        f"GRANT SELECT ON VIEW {fq}.cost_by_tag_v TO `{app_sp}`",
        f"GRANT SELECT ON VIEW {fq}.endpoint_billing_v TO `{app_sp}`",
        f"GRANT SELECT ON VIEW {fq}.endpoint_usage_v TO `{app_sp}`",
    ]
    for g in grants:
        try:
            spark.sql(g)
            print(f"ok: {g}")
        except Exception as ex:
            print(f"WARNING: {g} -> {ex}")
else:
    print("app_sp not provided — grant the app SP SELECT on the schema/views manually (see README).")

# COMMAND ----------

print("Setup complete.")
