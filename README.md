# jobwatch: new-job alerts for post-silicon / performance roles

Checks the careers sites of ~26 chip companies on a schedule. When a new job matches
your filters, it sends a push to your phone (tap it to open the posting) and/or an email.

- **First run:** quietly records every existing job, so you're not flooded. After that,
  you only hear about jobs posted since the last check.
- **Broken site detection:** if a company's site fails 3 runs in a row, you get a warning
  push instead of silence.

---

## 1. Set up phone alerts (2 min)

1. Install the free **ntfy** app (iOS App Store / Google Play).
2. Tap **+** → "Subscribe to topic" → enter a long random name, e.g.
   `uzair-jobs-8f3k2q9x`. Anyone who knows the name can see your alerts, so make it
   unguessable.
3. Keep that name. It's your `NTFY_TOPIC`.

Optional email: for Gmail you need an **App Password** (Google Account → Security →
2-Step Verification → App passwords). Your normal password won't work.

---

## 2A. Run it free on GitHub (recommended: runs when your PC is off)

1. Create a GitHub account and click **New repository**, e.g. `jobwatch`.
   - **Public** = unlimited free runs every 20 min. Nothing private is in these files;
     your topic and passwords live in Secrets (step 3), which are never shown.
   - **Private** = about 2,000 free minutes a month. Change the cron line in
     `.github/workflows/jobwatch.yml` to `"0 */3 * * *"` (every 3 hours) to stay within that.
2. **Add file → Upload files** → drag in everything from this folder, including the
   `.github` folder. Do not upload a `.env` file.
3. **Settings → Secrets and variables → Actions → New repository secret.** Add:
   - `NTFY_TOPIC` (required for phone pushes)
   - `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO` (optional, for email)
4. **Actions** tab → enable workflows → **jobwatch** → **Run workflow**.
   - The first run records existing jobs (no alerts).
   - Click into the run log to see which companies show ✓ or ✗.

From then on it runs every 20 minutes by itself. If GitHub ever pauses the schedule
after a long stretch of no activity, open Actions and re-enable it.

## 2B. Or run it on your Windows PC

```bat
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env        &REM then edit .env with your topic / email
python jobwatch.py --check    &REM tests every company, shows what matches
python jobwatch.py --test-alert
python jobwatch.py            &REM first real run: records existing jobs
```

Schedule it every 15 minutes (run once in Command Prompt, replacing the path):

```bat
schtasks /Create /SC MINUTE /MO 15 /TN JobWatch /TR "C:\path\to\jobwatch\run_windows.bat"
```

The PC has to be awake for this to work. Output goes to `jobwatch.log`.

---

## 3. First thing: run `--check`

```bat
python jobwatch.py --check
```

This shows each company with ✓/✗, how many jobs were found, and sample matching titles.

- Sources marked `[verified]` in `companies.yaml` were confirmed working.
- Sources marked `[check]` use each company's known careers-site setup, but couldn't be
  tested in advance. If one shows ✗:
  - **workday:** open the company's careers page, click a job, and copy `host` and `site`
    from the URL. Example: `https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/...`
    gives host `nvidia.wd5.myworkdayjobs.com`, tenant `nvidia`, site `NVIDIAExternalCareerSite`.
  - **browser:** check that the `url` still loads search results, and that `link` matches
    the job links on that page.
- Run one company at a time with `--only "Apple"`.
- Add `JOBWATCH_DEBUG=1` to see full errors.

**Tesla** blocks bots heavily and will probably fail. For Tesla, plus Google, Meta, Apple
and Microsoft, also turn on each company's own job alerts on its careers site as a backup.

## 4. Tuning

Everything lives in `companies.yaml`:

- `filters.include` / `exclude`: title keywords (whole word, any case). Remove `performance`
  if you get too many unrelated "performance" roles. Add terms like `architect` to widen.
- `filters.locations`: allowed places. Delete the list to allow anywhere.
- `search_queries` / `browser_queries`: what gets typed into each site's search box.
- **Adding a company:** if its job links contain `greenhouse.io/NAME`, `ashbyhq.com/NAME`
  or `lever.co/NAME`, add:
  ```yaml
  - name: SomeCo
    type: greenhouse        # or ashby / lever
    board: NAME
  ```
- To mute a company without deleting it, add `enabled: false`.
- To reset and re-record everything, delete `seen.json`.
