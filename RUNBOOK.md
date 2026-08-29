# Operational Runbook — Job Apply Automater

This runbook outlines operational procedures for the automated job application pipeline.

## 1. How to Manually Re-Run a Failed Job Description

If a job application fails midway (e.g., due to an ATS timeout or network error), it will be logged in your Google Sheet with the status `error`. Since the pipeline relies on the `apply_url` for deduplication, you can re-run the job by following these steps:

1. **Delete the Failure Row:** Open your Google Sheet and delete the row corresponding to the failed job. If you don't delete it, the `fetch_jds.py` step will see the `apply_url` and skip it on the next run.
2. **Trigger the Workflow:** Go to the **Actions** tab in your GitHub repository.
3. Select the `Job Apply Pipeline` workflow.
4. Click **Run workflow**. 
5. The pipeline will fetch the jobs again. Since you deleted the row, the failed job will no longer be considered "processed" and will be re-scored and applied to.

## 2. Fixing Broken ATS Selectors

ATS systems (Greenhouse, Lever, etc.) occasionally update their HTML structure, causing Playwright to timeout when trying to fill a form.

1. **Identify the Failure:** Your Telegram notification or GitHub Actions log will show `playwright.builtin.TimeoutError`.
2. **Reproduce Locally:** 
   - Pull the latest code.
   - Run `python scripts/auto_apply_ats.py --dry-run` to test the selectors locally without submitting real applications.
3. **Inspect the Form:**
   - Go to the job application URL in your browser.
   - Right-click the failing field (e.g., Resume upload button) and click **Inspect**.
   - Note the new `id`, `name`, or `class` attributes.
4. **Update the Script:**
   - Open `scripts/auto_apply_ats.py`.
   - Locate the function for the ATS (e.g., `_apply_greenhouse`).
   - Update the Playwright selector string to match the new HTML structure.
5. **Test and Push:**
   - Re-run the local `--dry-run` test.
   - If successful, commit and push the changes. The next automated run will use the new selectors.

## 3. Dealing with Groq Model Deprecations

If the pipeline fails with a Groq API `404` or `Model Not Found` error, the LLM model we are using has likely been deprecated.

1. Go to the [Groq Models Documentation](https://console.groq.com/docs/models) to find a currently supported model name (e.g., `llama-3.3-70b-versatile`).
2. Open your GitHub Repository.
3. Go to **Settings > Secrets and variables > Actions > Variables**.
4. Create or update a Repository Variable named `GROQ_MODEL` and set its value to the new model name.
5. The pipeline will automatically pick up this environment variable during the next run.

## 4. Bypassing Google Sheets Rate Limits

If the pipeline processes a massive burst of jobs, `gspread` might hit Google Sheets API write limits (429 errors). The `tenacity` retry logic with exponential backoff handles transient limits automatically. However, if the error persists:

- Check your Google Cloud Platform Console for the `Google Sheets API` quota.
- Consider batching writes (this would require modifying `log_and_notify.py` to use `sheet.append_rows(list_of_rows)` instead of appending row-by-row).
