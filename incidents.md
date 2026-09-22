# Incident log

What broke, how it was found, and what changed as a result. Kept because the
failures are more instructive than the code, and because one of them was a wrong
conclusion I published to myself before catching it.

---

## 2026-09-18 — A wrong finding: forecast compared against measurement

**Severity:** high — this one reached a conclusion before it was caught.

**What I concluded:** Open-Meteo understates haze by roughly 3x. On 18 Sep at 3pm it
reported ~40 µg/m³ for Petaling Jaya while IQAir measured 128 µg/m³ and the official
API of 164 implied a 24-hour average near 123. Running 40 µg/m³ through the DOE
formula gives API ~73, "Moderate", on a day when 30 areas nationwide were Unhealthy.

**Why it was wrong:** the fetch ran that same morning with `past_days=30` and
`forecast_days=5`. Everything from the model run onward is *forecast*, not analysis.
I had compared a forecast against a measurement and attributed the error to the
model's accuracy in general.

**How it was caught:** plotting the full series instead of reading single values. The
two curves interweave closely from 20 Aug to ~17 Sep, then diverge sharply at exactly
the model-run boundary.

**The actual finding, which is narrower and more interesting:** CAMS analysis tracks
community sensors well in the Klang Valley. Its forecast collapsed during this
episode, predicting clearing air about three days before it arrived.

**Changes made:**
- `make_charts.py` now shades the forecast period and labels the analysis/forecast
  boundary, so the distinction is visible rather than assumed.
- Open-Meteo is used for forecast *shape* only; its magnitudes are never presented as
  concentrations.

**Lesson:** never compare a single value across sources without checking what each
value actually represents.

---

## 2026-09-18 — Date filter silently ignored, 10,000 rows returned

**Symptom:** `fetch_openaq.py --days 30` returned 10,000 hours per sensor. Thirty days
is 720. 10,000 was exactly the internal page cap.

**Cause:** the OpenAQ v3 endpoint accepted `date_from` / `date_to` without error and
ignored them. The correct parameters are `datetime_from` / `datetime_to`. No 4xx, no
warning — the API returned everything and the pager kept going.

**Why it mattered:** the returned pages were the *oldest* rows, so the most recent
data — the haze episode itself — may have been absent entirely.

**Fix:** corrected the parameter names. Row counts dropped to 719 per sensor, which is
30 days minus missing hours.

**Lesson:** an API accepting a parameter is not the same as honouring it. Verify the
filter by checking the row count against what you expect, not by the absence of an
error.

---

## 2026-09-18 — Open-Meteo grid cells distinct, values identical

**Symptom:** three of four stations returned byte-identical PM2.5 series across 840
hours, despite Open-Meteo reporting four different grid cell coordinates.

**Cause:** Malaysia is served by the CAMS global model (~45 km) rather than the 11 km
European domain. Output is interpolated onto a finer grid, so coordinates differ while
the underlying values do not.

**Consequence:** Open-Meteo cannot resolve station-level differences across the Klang
Valley. Combined with the forecast problem above, this moved it out of the primary
data path entirely and made OpenAQ ground measurements the project's backbone.

**Lesson:** distinct coordinates in a response do not imply distinct data. Check the
values.

---

## 2026-09-21 — Postgres rejecting the correct password

**Symptom:** `psycopg2.OperationalError: password authentication failed` while
`docker compose exec postgres printenv POSTGRES_PASSWORD` showed the expected value.

**Cause:** Postgres reads `POSTGRES_PASSWORD` only when it initialises the data
directory. The password had been changed in `.env` after the volume already existed,
so the container's environment and the database's stored credential had diverged.

**Fix:** `ALTER USER haze PASSWORD '...'` inside the container, then aligned `.env`.
Recreating the volume would also work but destroys data.

**Lesson:** environment variables configure creation, not ongoing state.

---

## 2026-09-21 — Port 5433 answered by the wrong process

**Symptom:** authentication still failing after the password was verified correct
inside the container.

**Cause:** `netstat -ano | findstr :5433` showed two processes listening. A separate
Postgres installation on the host (bundled with Pentaho) was answering first. The
loader had been authenticating against a different database entirely.

**Fix:** moved the container to 5434 and pinned `POSTGRES_HOST=127.0.0.1` so
connections resolve over IPv4 to Docker rather than to an IPv6 listener on the host.

**Lesson:** "connection refused" and "authentication failed" can both mean you reached
the wrong server. Check what is listening before debugging credentials.

---

## 2026-09-18 — Google Sheets incremented the year on autofill

**Symptom:** hand-collected readings taken on one afternoon carried dates of
18/9/2026, 18/9/2027, 18/9/2028, 18/9/2029.

**Cause:** dragging to autofill a date column incremented the year rather than copying
the value.

**Impact:** the validation join matched nothing, because sensor data existed only for
2026.

**Fix:** corrected by hand; dates are now typed rather than dragged. `load_manual.py`
parses day-first explicitly and prints every parsed row for review before writing.

**Lesson:** hand-collected data needs validation at the point of entry. This was
caught by eye, which does not scale.

---

## 2026-09-18 — Index scales conflated

**Symptom:** IQAir showed 204 for Petaling Jaya while APIMS showed 164. Both were
recorded in the spreadsheet as "Malaysia API".

**Cause:** IQAir reports US AQI. Malaysia's API and the US AQI use different breakpoint
tables — the US splits 101–200 into two bands where Malaysia has one.

**How it was confirmed:** applying the US EPA PM2.5 breakpoints to IQAir's own reported
concentrations reproduced its index exactly for all four stations (128.3 → 204,
92.9 → 177, 81.1 → 169, 79.7 → 168). Applying the Malaysian formula to the same
128.3 µg/m³ gives 171, not 204.

**Fix:** `index_type` is now a required column, and `load_manual.py` warns when an
IQAir row is labelled as a Malaysian API reading.

**Lesson:** two numbers describing the same air are not comparable until you know which
scale each uses. Concentration is the only thing comparable across systems.

---

## 2026-09-20 — Weekend readings missed

**Symptom:** no APIMS observations for 19–20 September.

**Cause:** the collection step depended on a person remembering. It failed on its first
weekend.

**Impact:** permanent. APIMS keeps no public archive, so those hours cannot be
recovered. The sensor data for the same period survived, because OpenAQ retains
history.

**Fix:** none yet. Automated capture of APIMS is the highest-priority outstanding item;
manual collection continues as a stopgap.

**Lesson:** a pipeline step that depends on human memory is not a pipeline step.

---

## Smaller items

- **Colab `%%writefile` wrote 1-byte files.** The magic consumes the rest of *its own*
  cell; pasting the content into a separate cell produced an empty file that ran with
  no output and no error. Checking `ls -la` for plausible file sizes catches it.
- **`ModuleNotFoundError` for a file that existed.** Python caches the directory
  listing at interpreter start, so a module created afterwards is invisible until
  `importlib.invalidate_caches()`.
- **Sensors report ~97% of hours, not 100%.** Roughly 20 hours missing per 30 days per
  sensor (power, connectivity). The 24-hour rolling average therefore requires a
  minimum of 18 readings in the window, or it yields nothing rather than a value
  computed from too little data.
