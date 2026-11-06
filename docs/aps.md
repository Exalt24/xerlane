# APS connector (Autodesk Platform Services)

Pulls model object data from Autodesk Platform Services into the same PostgreSQL schema the XER loader uses, with the same safety properties: an idempotent re-run, a change log, a quarantine for bad rows and row level security per tenant.

## What was and was not tested

**Written from the documentation and tested against a stand-in. It has never been run against a live Autodesk account.** We hold no APS credentials. The client follows the vendor's published OpenAPI files; `tests/aps_stand_in.py` is a small localhost server that serves exactly those routes and shapes, so the tests prove the client does what the documentation says. They cannot prove that Autodesk answers the way the documentation says. Where the documentation was silent or unclear the code says so, and the list is under "Not confirmed" below.

A live run needs a client id and secret from an APS app (the "Server-to-Server" two-legged kind), put in the environment and never in the repo:

```
APS_CLIENT_ID=...
APS_CLIENT_SECRET=...
```

Optional: `APS_REGION` (`US`, `EMEA` or `AUS`, sent as the `region` header) and `APS_BASE_URL` (for a stand-in; plain `http` is accepted for localhost only, everything else must be `https`, because the credentials travel in the request).

## Sources used

- Token: `POST /authentication/v2/token`, request and response shapes from the `aps-sdk-openapi` repository, `authentication/authentication.yaml` (HTTP Basic client id and secret, form body `grant_type=client_credentials` and `scope`, answer `access_token`, `token_type`, `expires_in`; 400 and 401 for bad requests and credentials; rate limit 500 per minute).
- Model Derivative: `modelderivative/modelderivative.yaml` in the same repository, for `GET /modelderivative/v2/designdata/{urn}/metadata` (List Model Views: `data.metadata[]` with `name`, `role`, `guid`, `isMasterView`) and `GET .../metadata/{modelGuid}/properties` (Fetch All Properties: `data.collection[]` with `objectid`, `name`, `externalId`, `properties`; `202 {"result": "success"}` means "repeat the request until you get 200"; `objectid` is non-persistent, `externalId` is the persistent id).
- Rate limits: the "APS Rate Limits and Quotas" and "Model Derivative Rate Limits" pages of the Model Derivative developer guide: HTTP 429 with a `Retry-After` header in seconds and a body such as `{"developerMessage":"Quota limit exceeded."}`; 300 requests per minute on the properties endpoints when a request starts a metadata extraction.

The `aps.autodesk.com` reference pages render their request and response sections with JavaScript, so a plain page fetch shows only the headings; the OpenAPI files are what those pages are generated from.

## Parts

| Part | Where | What it does |
|---|---|---|
| `ApsClient` | `xerlane/aps.py` | Two-legged OAuth with a cached bearer token refreshed a little before expiry, `list_views(urn)`, `get_properties(urn, guid)`. Standard library `urllib` only. |
| `map_collection` | same | One properties collection to rows to store and items to set aside, each with a reason. |
| `load_view` | same | Upserts one model view for one tenant in one transaction. |
| `sync_model` | same | List the views of a model, fetch each, load each. |
| `reconcile` | same | APS objects with no XER task, XER tasks with no APS object. Reads both sides, changes neither. |
| `aps_schema.sql` | `xerlane/` | Four tables with FORCED row level security, plus the join view `aps_object_task`. |
| `python -m xerlane.aps` | same | `init-schema`, `sync`, `reconcile`. |

### Client rules

- Token: fetched on first use and again once the clock passes `expires_in` minus a 120 second margin (halfway for a token that lives less than four minutes). A 401 on an API call drops the token, fetches a new one and retries that call once; a second 401 is an `ApsAuthError`.
- 202 on the properties route: asked again after 1, 2, 4, ... seconds (capped at 15), at most 12 waits, then `ApsNotReady`. Polling does not use up the error retries.
- 429: waits exactly `Retry-After` seconds. A missing or unreadable header falls back to the backoff below. A `Retry-After` over 120 seconds raises `ApsRateLimited` at once instead of hanging.
- 5xx and network errors: retried after 0.5, 1, 2, ... seconds (capped at 10), at most 5 retries, then the error is raised with its status.
- An answer that ends before its `Content-Length` bytes arrived is treated as a network error and retried, never parsed; a response over 256 MiB is refused.
- Every other 4xx, including 403, is final and never retried. A redirect is never followed, so the bearer token cannot be sent to another host.
- The `urn` and the view `guid` must be URL-safe characters before they are put in a path. `urn_from_object_id` builds the URL-safe Base64 form Model Derivative expects.
- No token, secret or Basic header is logged or placed in an error message; text that echoes one back from a server error body is replaced with `***`. A test drives a busy run and searches every log line and exception for them.

### What is stored

`aps_object` holds one row per model object, keyed by tenant, model urn, view guid and `objectid`: `external_id`, `name`, `category`, the nullable `task_code`, all `properties` as jsonb, and a `content_hash` over everything stored about the row. `aps_load_run` has one row per load that changed anything, `aps_change_event` one row per object added, changed (with the column names that changed) or removed, `aps_quarantine` the items that failed validation.

`task_code` is read from a property named `task_code` or `Task Code`, at the top of the object's properties or one group down (APS groups properties by category). The names can be passed to `map_collection` and `load_view` as `task_code_keys`. The view `aps_object_task` joins an object to the XER `task` with the same `task_code`; a task code is unique only inside a project, so with several projects loaded filter on `proj_id`.

### Validation (the quarantine)

An item is set aside, never dropped, and the rest of the view still loads:

| Reason code | When |
|---|---|
| `MALFORMED_OBJECT` | the entry is not a JSON object |
| `NO_OBJECT_ID` | no `objectid` |
| `BAD_OBJECT_ID` | `objectid` is not a whole number from 0 to 2**63-1 |
| `BAD_FIELD_TYPE` | `name` or `externalId` is not text |
| `BAD_PROPERTIES` | `properties` is not a JSON object |
| `BAD_CHARACTER` | text holds a NUL or an unpaired surrogate, which Postgres cannot store |
| `VALUE_TOO_LARGE` | `name` or `externalId` over 2048 characters, or the properties over 64 KiB |
| `DUP_OBJECT_ID` | the same `objectid` appears twice in one view; every copy is set aside, none is stored, because there is no way to tell which one is right |

The quarantine keeps the raw item when it is under 4 KiB and a short stub (`truncated` or `unstorable`, with the object id when there is one) otherwise.

### Idempotence and the change log

A load computes the difference between the stored view and the accepted items: new ids are inserted, ids whose `content_hash` differs are updated, stored ids missing from the payload are deleted, each with an event under one `aps_load_run` row. The same payload again finds no difference and writes nothing: no run, no event, no row version, no sequence value. A payload that holds no valid object while the view has stored objects would delete them all, so it is refused (`allow_empty=True` or `--allow-empty` overrides). Two loads of the same view take turns on a per view advisory lock.

## Running it

```
python tools/pg.py up
python -m xerlane.aps init-schema
APS_CLIENT_ID=... APS_CLIENT_SECRET=... python -m xerlane.aps sync --tenant acme --urn <base64url urn> --roles 3d
python -m xerlane.aps reconcile --tenant acme --proj-id 1
```

`sync` exits 0 when every view loaded, 1 when a view failed (the others are still loaded), 2 when the run could not start. `reconcile` exits 0 when everything lines up and 1 when it does not.

The model must already be translated to SVF or SVF2 (Model Derivative only serves metadata of a translated design); this connector reads, it does not start translation jobs.

## Tests

```
python -m pytest tests/test_aps_map.py tests/test_aps_client.py tests/test_aps_db.py tests/test_aps_property.py
python tests/mutation_check_aps.py            # every mutant of xerlane/aps.py, 4 workers
python tests/mutation_check_aps.py --summary  # totals from the recorded run
```

The client and mapping tests need no network and no database. The database tests need the Postgres from `python tools/pg.py up` and fail, never skip, without it. They run in their own database (`xerlane_aps_test`, or the name in `XERLANE_APS_TEST_DB`) so they never wipe the tables of the XER tests.

`tests/test_aps_property.py` plays seeded random worlds (several tenants, models and views; edits, bad items, duplicates, replays and empty payloads) against an independently written oracle, and checks after every step that the stored rows, the reported counts, the no-op replay, the isolation between slots, the replayed change log and the quarantine all agree.

## Not confirmed

These are choices made where the documentation did not settle the question. Each is one place in `xerlane/aps.py`.

1. **Where the category lives.** The Model Derivative reference does not name a property for an object's category. The code looks for `Category` inside a `__category__` group, then at the top level, and stores nothing otherwise. Check against a real translation.
2. **The task code property.** There is no standard APS property for a schedule activity; `task_code` / `Task Code` is our convention. A model that carries the link under another name needs `task_code_keys`.
3. **Large models.** Without the `forceget` query parameter APS does not return resources over 20 MB, and the response is not paginated on this route (Fetch Specific Properties is). The client reads one response up to 256 MiB and does not send `forceget`. A model that needs either would need a paginated path.
4. **`objectid` is not persistent.** APS assigns it at translation time, so a re-translated design can renumber every object. The table key is `objectid` as the documentation's per-view id, so a re-translation shows as many removals and additions; `external_id` is stored so a reader can match them. Re-keying on `external_id` would be the next step if the models in question carry it for every object.
5. **Legacy SVF ids.** The `x-ads-derivative-format` header (SVF2 or legacy SVF object ids for BIM 360 / Forma designs) is not sent.
6. **Archdesk.** The job post names it next to APS. No connector for it exists here; it was not researched.
