# n8n workflows

Six workflows, exported from n8n and cleaned. They hold no keys, tokens, webhook paths or credential ids. Every
placeholder is a plain `YOUR_...` string you replace after import. They are imported inactive, so nothing runs until you turn it on.

| File | What it does |
|---|---|
| `01_sourcing_events_to_leads.json` | Weekly. Reads organiser speaker pages after a robots.txt check, scores each person with the qualification tree, calls `ingest_leads`. |
| `02_sourcing_rendered_pages_firecrawl.json` | Same, for pages that only render with JavaScript. Uses Firecrawl. |
| `03_approved_leads_enrich_hubspot_dry_run.json` | Runs when a lead is approved (webhook) and hourly as a sweep. Claims leads with `claim_approved`, enriches them (Apollo, Clay), builds the HubSpot payload as a dry run, saves with `save_enrichment`. Also calls `dlq_replay`. |
| `04_clay_results_contact_email.json` | Receives the Clay result, verifies the email with Hunter, calls `save_contact`. |
| `05_feedback_outreach_events.json` | Receives Smartlead and Instantly webhooks and calls `log_outreach_event`. Not live until sending starts. |
| `06_error_handler_dlq.json` | Error workflow. Writes any unhandled failure to the dead-letter queue with `log_dlq`. |

## Set up after importing

1. **Fill the Config node** in each workflow (a Code node named `Config`):
   - `YOUR_SUPABASE_URL` and `YOUR_SUPABASE_PUBLISHABLE_KEY`: Supabase, Project Settings, API.
   - `YOUR_INGEST_TOKEN`: the value you stored as `ingest_token` in `app_secrets` (see the bottom of `sql/04_security.sql`).
   - `YOUR_CLAY_WEBHOOK_URL` and `YOUR_CLAY_TABLE_URL`: from your Clay table (workflows 03, 04, 05).
2. **Set a webhook path** on each Webhook node (workflows 03, 04, 05). Replace `REPLACE_WITH_A_LONG_RANDOM_PATH` with a long random string. The path is the only thing protecting the endpoint, so never reuse one that has been shared.
3. **Reconnect credentials.** Exports carry no credentials, so pick them on these nodes:

   | Node | Credential |
   |---|---|
   | `Apollo search`, `Apollo hiring` (03) | Header Auth with your Apollo key |
   | `Firecrawl`, `Firecrawl exhibitors` (02) | Header Auth with `Authorization: Bearer <key>` |
   | `Hunter verify` (04) | Hunter account |
4. **Set the error workflow.** In workflows 01 to 05, open Settings and choose `06_error_handler_dlq` as the error workflow.
5. **Edit the pitch** in the `Build` node of workflow 03: replace `YOUR_COMPANY` and the two `PITCH_` lines with what your company does.
6. **Point the database at the approval webhook.** Put the production URL of the workflow 03 Webhook node into `notify_approved()` in `sql/03_functions.sql`.
7. Turn the workflows on one at a time, starting with 01, and watch the first run.

Before you commit a new export, run `scripts/check_secrets.sh`. n8n keeps the keys you type straight into a node, so a clean-looking export can still hold one.

## Names that look odd

`gm_lead_id`, `gm_market` and `gm_committee_role` are the custom-field names the outreach tools and the CRM payload use to carry the lead id back. They are used in `log_outreach_event()` in `sql/03_functions.sql` and in workflows 03 and 05. If you rename them, rename them in all three places.
