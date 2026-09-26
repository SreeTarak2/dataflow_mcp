# 🎯 Migration Tools Quick Reference

## The 4 New Migration Tools

---

## 1️⃣ **get_migration_status**

### Purpose
Check how many of your 810 contests need migration.

### Parameters
None

### Example Call
```bash
Tool: get_migration_status
```

### Example Response
```json
{
  "success": true,
  "total_contests": 810,
  "migrated_count": 450,
  "migration_percentage": 55.56,
  "needs_migration": 360,
  "breakdown": {
    "missing_canonical_category": 200,
    "missing_prize_summary": 180,
    "missing_fee_confidence": 150
  }
}
```

### What This Tells You
- ✅ 450 contests already have v4.0 fields
- ⏳ 360 contests still need updates
- 🔴 Most missing: canonicalCategory (200)

### When to Use
- **First thing:** See overall progress
- **Every N batches:** Track migration completion
- **Final check:** Verify 100% completion

---

## 2️⃣ **get_contests_for_migration**

### Purpose
Fetch a batch of unmigrated contests ready for normalization.

### Parameters
- `batch_size` (1-100, default 10)
- `skip` (0, 10, 20, ..., default 0)

### Example Call
```bash
Tool: get_contests_for_migration
Parameters:
  batch_size: 10
  skip: 0
```

### Example Response (abbreviated)
```json
{
  "success": true,
  "contests": [
    {
      "_id": "65f8a1b2c3d4e5f6g7h8i9j0",
      "title": "45th Uppsala Short Film Festival",
      "category": "Filmmaking",
      "description": "Sweden's premier short film arena invites global entries...",
      "image": {
        "primary": {
          "url": "https://example.com/image.jpg",
          "source": "external",
          "status": "active"
        },
        "backup": null,
        "alt": "Film festival logo"
      },
      "prize": {
        "isMonetary": true,
        "originalAmount": "€20,000",
        "totalUSD": 0,
        "currency": "EUR",
        "description": "Cash prizes for winners"
      },
      "entry": {
        "isFree": false,
        "fee": {
          "amount": 0,
          "currency": "USD"
        },
        "feeConfidence": "unknown"
      },
      "audience": {
        "skillLevels": ["beginner", "intermediate", "advanced"],
        "primarySkillLevel": "intermediate",
        "eligibilityLabel": "Short filmmakers worldwide",
        "location": "Online"
      },
      "timeline": {
        "submissionDeadlineUTC": "2026-03-01T18:00:00"
      },
      "tags": ["short-film", "cinema"],
      ...
    },
    ... 9 more contests ...
  ],
  "count": 10,
  "total_needing_migration": 360,
  "skip": 0,
  "batch_size": 10
}
```

### What's in the Response
- ✅ 10 contest JSON objects (full data)
- ✅ Pagination metadata
- ✅ Total needing migration

### Next Steps After Getting This
1. Copy the contests JSON
2. Paste into Claude with Prompts-backfill.txt
3. Get normalized patches as output
4. Use `apply_migration_patch` or `bulk_apply_migrations`

### When to Use
- **Step 1 of migration:** Get first batch
- **Step 3 of pagination:** Get next batch (skip += 10)
- **Spot checks:** Get random batches

### Pagination Example
```
Batch 1: skip=0,   batch_size=10  → contests 0-9
Batch 2: skip=10,  batch_size=10  → contests 10-19
Batch 3: skip=20,  batch_size=10  → contests 20-29
...
Batch 81: skip=800, batch_size=10 → contests 800-809
```

---

## 3️⃣ **apply_migration_patch**

### Purpose
Apply a normalized patch to a SINGLE contest.

### Parameters
- `contest_id` (string) — MongoDB ObjectId from get_contests_for_migration
- `patch_json` (string) — JSON from Claude's backfill prompt output

### Example Call
```bash
Tool: apply_migration_patch
Parameters:
  contest_id: "65f8a1b2c3d4e5f6g7h8i9j0"
  patch_json: '{"canonicalCategory": "Writing & Media", "prize": {"prizeSummary": "Cash prizes totaling €20,000"}, "tags": ["oscar-qualifying"]}'
```

### Example Response
```json
{
  "success": true,
  "contest_id": "65f8a1b2c3d4e5f6g7h8i9j0",
  "modified_count": 3,
  "message": "Patch applied successfully"
}
```

### What Happens Under the Hood
1. Validates ObjectId format
2. Parses patch JSON
3. Merges patch into contest (only specified fields updated)
4. Adds `_migration_updated_at` timestamp
5. Returns success or error

### Important Notes
- ✅ Only patches specified in patch_json are updated
- ✅ All other fields remain unchanged
- ✅ Can run multiple times safely (idempotent)
- ❌ contest_id must be valid MongoDB ObjectId
- ❌ patch_json must be valid JSON string

### When to Use
- **Single contest:** Process one at a time (slow)
- **Manual review:** Verify each patch before applying
- **Testing:** Test migration logic before bulk

### Speed
- ~1 second per patch (database time only)

---

## 4️⃣ **bulk_apply_migrations**

### Purpose
Apply multiple patches at once (FASTER).

### Parameters
- `migrations_json` (string) — Array of {contest_id, patch} objects

### Example Call
```bash
Tool: bulk_apply_migrations
Parameters:
  migrations_json: '[
    {
      "contest_id": "65f8a1b2c3d4e5f6g7h8i9j0",
      "patch": {
        "canonicalCategory": "Writing & Media",
        "prize": {"prizeSummary": "Cash prizes totaling €20,000"},
        "tags": ["oscar-qualifying"]
      }
    },
    {
      "contest_id": "65f8a1b2c3d4e5f700000001",
      "patch": {
        "canonicalCategory": "Technology & AI",
        "prize": {"prizeSummary": "AWS credits"},
        "feeConfidence": "confirmed"
      }
    },
    ... more contests ...
  ]'
```

### Example Response
```json
{
  "success": true,
  "total": 10,
  "successful": 9,
  "failed": 1,
  "details": [
    {"contest_id": "id1", "status": "success"},
    {"contest_id": "id2", "status": "success"},
    {"contest_id": "id3", "status": "success"},
    {"contest_id": "id4", "status": "failed", "error": "Contest not found"},
    {"contest_id": "id5", "status": "success"},
    ...
  ]
}
```

### What Happens Under the Hood
1. Validates array format
2. For each migration:
   - Validates ObjectId
   - Parses patch JSON
   - Applies update
   - Records result
3. Returns summary + details

### Important Notes
- ✅ Partial failures OK (9/10 succeed, 1 fails)
- ✅ Failed contests don't affect others
- ✅ Much faster than single operations
- ❌ Large arrays (1000+) may timeout
- ❌ Each patch_json must be valid

### When to Use
- **Batch processing:** Apply 10-100 patches together
- **Speed:** 10x faster than single operations
- **Efficiency:** Better resource utilization
- **Production:** Recommended for full migration

### Speed Comparison

| Approach | 10 Contests | 100 Contests | 810 Contests |
|----------|------------|-------------|-------------|
| Single | 10 sec | 100 sec | 810 sec (~13 min) |
| Bulk 10 | 1 sec | 10 sec | ~80 sec |
| Bulk 50 | <1 sec | 2 sec | ~16 sec |

---

## 🔄 Complete Workflow Example

### Scenario: Migrate 30 contests

### Step 1: Check Status
```bash
Tool: get_migration_status
→ Response: needs_migration: 360
```

### Step 2: Get First Batch (10 contests)
```bash
Tool: get_contests_for_migration
Parameters: batch_size=10, skip=0
→ Response: Array of 10 contest JSON objects
```

### Step 3: Normalize with Claude
**Input to Claude:**
```
[Copy prompts/Prompts-backfill.txt]

[Paste full 10 contests JSON from Step 2]

Output: Provide patches for each contest that needs updating.
```

**Example Claude Output:**
```json
[
  {
    "contest_id": "65f8a1b2c3d4e5f6g7h8i9j0",
    "patch": {
      "canonicalCategory": "Writing & Media",
      "prize": {"prizeSummary": "Cash prizes totaling €20,000"},
      "tags": ["oscar-qualifying"]
    }
  },
  {
    "contest_id": "65f8a1b2c3d4e5f700000001",
    "patch": {
      "canonicalCategory": "Technology & AI",
      "prize": {"prizeSummary": "AWS credits"},
      "feeConfidence": "extracted"
    }
  },
  ...more...
]
```

### Step 4a: Apply Patches (Bulk)
```bash
Tool: bulk_apply_migrations
Parameters: migrations_json: [10 patches from Claude]
→ Response: 10 successful, 0 failed
```

### Step 4b: Or Apply Single (for testing)
```bash
Tool: apply_migration_patch
Parameters: contest_id, patch_json
→ Response: 1 successful update
```

### Step 5: Get Next Batch
```bash
Tool: get_contests_for_migration
Parameters: batch_size=10, skip=10
→ Response: Next 10 contests
```

### Step 6: Repeat Steps 3-5
- Process contests 10-20 batch
- Process contests 20-30 batch
- Total: 30 contests migrated

### Step 7: Verify Progress
```bash
Tool: get_migration_status
→ Response: migration_percentage increased, needs_migration decreased
```

---

## 💡 Pro Tips

### Bulk Processing Strategy
```
For 810 contests: Process in batches of 50

Batch 1: skip=0,   batch_size=50
Batch 2: skip=50,  batch_size=50
...
Batch 17: skip=800, batch_size=10 (last partial batch)

Total: ~17 Claude calls + ~17 bulk migrations = ~1-2 hours
```

### Rate Limiting
```
If you get "Rate limit exceeded":
- Default: 100 req/min
- Wait 60 seconds OR
- Use larger batch sizes (50 instead of 10)
```

### Error Recovery
```
If 1 out of 10 patches fails:
- bulk_apply_migrations handles it gracefully
- successful: 9, failed: 1
- No need to re-run all 10 — just fix the 1
- Or re-run all 10 (idempotent operation)
```

### Verification
```
After migration:
1. Check status: get_migration_status
2. Should show: migration_percentage: 100
3. Verify specific contest: read_collection("Contests", filter, limit=1)
```

---

## 📊 Typical Timeline for 810 Contests

```
Batch 1 (10 contests):  2-3 minutes
  - Fetch: 1 sec
  - Claude normalize: 1-2 min
  - Apply bulk: <1 sec

Batch 2-80 (similar):  ~2.5 min each

Total for 810:
  - Single approach: 6-8 hours
  - Bulk by 10: 3-4 hours ✅
  - Bulk by 50: 1-2 hours ✅✅
```

**Bottleneck:** Claude normalization time, not MCP server

---

## ⚠️ Common Mistakes

### ❌ Don't
```
# Submitting large arrays that will timeout
bulk_apply_migrations with 500+ patches in one call
→ Use batches of 50-100 instead

# Forgetting to validate patch format
patch_json: "{invalid json}"
→ Ensure valid JSON before calling

# Reusing contest_id incorrectly
contest_id: "{...ObjectId...}"  (as object)
→ Use string: "65f8a1b2c3d4e5f6g7h8i9j0"
```

### ✅ Do
```
# Submit manageable batches
bulk_apply_migrations with 50 patches
→ Complete in seconds

# Validate patch JSON
patch_json: '{"field": "value"}'  (as string)
→ Valid JSON string format

# Use correct ObjectId format
contest_id: "65f8a1b2c3d4e5f6g7h8i9j0"
→ 24 hex characters as string
```

---

## 🎯 Summary

| Tool | Use Case | Speed | Batch Size |
|------|----------|-------|-----------|
| **get_migration_status** | Check progress | Instant | N/A |
| **get_contests_for_migration** | Fetch to normalize | <1 sec | 1-100 |
| **apply_migration_patch** | Single patch | 1 sec | 1 |
| **bulk_apply_migrations** | Multiple patches | <1 sec | 10-100 ✅ |

**Recommended:** Use `bulk_apply_migrations` with batch_size=50 for fastest migration

---

**Ready to migrate? Start with `get_migration_status` to see your current state! 🚀**

---

# 🚀 Full Generation Pipeline (Raw → Structured + Details in One Pass)

Two new tools that bridge the gap between raw scraped data and fully published contest details in a single AI round-trip.

---

## 5️⃣ **get_records_for_full_generation**

### Purpose
Fetch raw scraped records + BOTH prompts (Prompts.txt for structuring + Prompts-contest-details.txt for marketing copy) so a chatbot can structure AND generate contest details in one pass.

### Parameters
- `source` (string, optional) — Filter by scraper source (e.g. "contestwatchers", "opportunityDesk"). If omitted, all sources.
- `limit` (integer, 1-10, default 3) — Maximum raw records to fetch. Lower limit recommended since each record requires full structuring + web research + detail generation.
- `require_validated` (boolean, default false) — If true, only fetch records with validationStatus="validated".

### Example Call
```bash
Tool: get_records_for_full_generation
Parameters:
  source: "contestwatchers"
  limit: 3
  require_validated: false
```

### Example Response (abbreviated)
```json
{
  "success": true,
  "record_count": 3,
  "records": [
    {
      "_id": "65f8a1b2c3d4e5f6g7h8i9j0",
      "title": "2026 Global Innovation Challenge",
      "url": "https://example.com/challenge",
      "source": "contestwatchers",
      "scrapedAt": "2026-07-25T10:00:00Z"
    },
    ... 2 more records ...
  ],
  "structuring_prompt_name": "Prompts.txt",
  "structuring_prompt": "(full Prompts.txt content)",
  "details_prompt_name": "Prompts-contest-details.txt",
  "details_prompt": "(full Prompts-contest-details.txt content)",
  "usage": {
    "purpose": "Send each record to your LLM with BOTH prompts. First structure using Prompts.txt, then research and generate details using Prompts-contest-details.txt.",
    "expected_output": "A JSON object with 'items' array, each item having 'record' and 'details'. Submit via submit_full_generation."
  }
}
```

### What's in the Response
- ✅ Raw scraper records (with URLs for the AI to research)
- ✅ Full Prompts.txt content (structuring schema & rules)
- ✅ Full Prompts-contest-details.txt content (detail generation rules)
- ✅ Usage instructions telling the AI what to return

### When to Use
- **Fast path:** Raw → published in one pass (instead of get_records_for_structuring → submit_structured_records → get_contests_for_detail_generation → submit_contest_details)
- **New sources:** First batch from a new scraper
- **Quick turnarounds:** When you want full contest pages generated quickly

### Limitations
- ❌ Max 10 records per call (AI has to do structing + research + detail gen for each)
- ❌ No dedup against existing contest_details (details are always versioned)

---

## 6️⃣ **submit_full_generation**

### Purpose
Submit a full generation result that includes BOTH structured contest data AND contest details in one call. This tool:
1. Upserts each structured record into the Contests collection (same dedup logic as submit_structured_records)
2. Finds the resulting contest `_id` by dedup key (source.name + title)
3. Validates and saves contest_details for each

### Parameters
- `generation_json` (string) — JSON string with an `items` array, where each item has:
  - `record`: Structured contest data following Prompts.txt v4.0 schema
  - `details`: Contest details following Prompts-contest-details.txt schema

### Example Call
```bash
Tool: submit_full_generation
Parameters:
  generation_json: '{
    "items": [
      {
        "record": {
          "title": "2026 Global Innovation Challenge",
          "link": "https://example.com/challenge",
          "type": "contest",
          "source": {"name": "contestwatchers"},
          "category": "Technology & AI",
          "prize": {
            "isMonetary": true,
            "totalUSD": 50000,
            "prizeSummary": "Cash prizes totaling $50,000"
          },
          "audience": {
            "eligibilityLabel": "Open to innovators worldwide",
            "location": "Worldwide"
          },
          "timeline": {
            "submissionDeadlineUTC": "2026-12-31T23:59:59"
          }
        },
        "details": {
          "content": {
            "hero": {
              "subheadline": "A $50,000 competition for global innovators",
              "valueProposition": "Solve real-world challenges and win funding"
            },
            "whyJoin": "This challenge brings together...",
            "whoShouldApply": "Ideal for tech entrepreneurs...",
            "benefits": ["$50,000 grand prize", "Global recognition", "Mentorship program"],
            "tips": ["Focus on scalability", "Include a working prototype"],
            "shouldYouApply": {
              "idealFor": "Early-stage startups with a working prototype",
              "goodFit": ["Have a MVP ready", "Team of 2+"],
              "notIdealFor": ["Idea-stage only", "Solo founders without technical co-founder"]
            },
            "readingTime": 3
          },
          "seo": {
            "metaTitle": "2026 Global Innovation Challenge - Apply Now",
            "metaDescription": "$50,000 prize for innovators solving global challenges. Open worldwide.",
            "keywords": ["innovation", "startup", "challenge", "funding"]
          }
        }
      }
    ]
  }'
```

### Example Response
```json
{
  "success": true,
  "total_items": 1,
  "successful": 1,
  "errors": 0,
  "results": [
    {
      "index": 0,
      "contest_id": "65f8a1b2c3d4e5f6g7h8i9j0",
      "is_new": true,
      "title": "2026 Global Innovation Challenge",
      "details_saved": true,
      "version": 1
    }
  ],
  "error_details": []
}
```

### What Happens Under the Hood
1. Parses the `items` array
2. For each item:
   - Validates required fields (title, link)
   - Maps the `record` to Contests schema via `_build_normalized_record` (same as submit_structured_records)
   - Upserts into Contests collection (dedup by source.name + title)
   - Queries the contest `_id` from the upsert
   - Validates the `details` content (quality checks)
   - Saves contest_details with automatic versioning
3. Returns per-item results + summary

### Important Notes
- ✅ Partial failures OK — each item is processed independently
- ✅ Record is saved to Contests even if details generation fails
- ✅ Details are versioned — a bad version can be superseded
- ❌ `record` must have `title` and `link` (required fields)
- ❌ `details` must have meaningful content (>50 words across sections)

### When to Use
- **After get_records_for_full_generation:** Submit the AI's combined output
- **Bulk imports:** Process multiple contests in one call

### Speed Comparison vs. Separate Pipeline

| Approach | 3 Contests | 10 Contests |
|----------|-----------|------------|
| Separate (structuring → details) | 2 AI calls + 2 submit calls | 2 AI calls + 2 submit calls |
| Full Generation (combined) | 1 AI call + 1 submit call | 1 AI call + 1 submit call |

**Bottleneck:** AI research & generation time, not server processing

---

## 🆚 Full Generation vs. Separate Pipeline

### Separate Pipeline (original)
```
1. get_records_for_structuring → AI structures → submit_structured_records
2. get_contests_for_detail_generation → AI researches → submit_contest_details
```
- ✅ Each step has focused prompts and validation
- ✅ Easier to debug (each step produces independent output)
- ❌ Two AI round-trips per contest
- ❌ Only works with contests already in Contests collection

### Full Generation (new)
```
1. get_records_for_full_generation → AI structures + researches → submit_full_generation
```
- ✅ One AI round-trip from raw to published
- ✅ Works directly with raw scraped data
- ✅ Both prompts available simultaneously (AI can cross-reference)
- ❌ More work per AI call (longer context, more instructions)
- ❌ Higher quality variance (AI has to do everything at once)

**Recommendation:** Use full generation for speed, separate pipeline for quality control.

---

## 🔄 Complete Full Generation Workflow

### Step 1: Check What's Available
```bash
Tool: get_scraped_overview
→ See which sources have raw data ready
```

### Step 2: Fetch Records + Both Prompts
```bash
Tool: get_records_for_full_generation
Parameters: source="contestwatchers", limit=3
→ Response: 3 records + Prompts.txt + Prompts-contest-details.txt
```

### Step 3: AI Structures + Generates Details
**Input to AI (one call):**
- Raw record data
- Prompts.txt (structuring schema)
- Prompts-contest-details.txt (detail generation rules)

**Expected Output:**
```json
{
  "items": [
    {
      "record": { ... structured contest data ... },
      "details": { ... contest details ... }
    }
  ]
}
```

### Step 4: Submit Both at Once
```bash
Tool: submit_full_generation
Parameters: generation_json: (AI output from Step 3)
→ Response: Contest saved to Contests + contest_details created with version 1
```

### Step 5: Verify
```bash
Tool: read_collection("contest_details", filter, limit=1)  # Inspect saved details
Tool: get_document("Contests", document_id)  # Check contest was created/updated
```

---

## 💡 Pro Tips

### When to Use Full Generation vs. Separate
```
USE FULL GENERATION:
- New scraper sources (no existing Contests data)
- Prototyping / exploring new data
- Quick volume: need many detail pages fast
- Single-pass workflows

USE SEPARATE PIPELINE:
- Existing contests needing detail pages
- Quality-critical: want focused prompts
- Debugging: need to isolate issues
- When humans review each step
```

### Rate Limiting
```
Same as other tools: 100 req/min shared pool
Full generation is 1 call vs 2 calls + 2 submits = 4 calls
→ 75% fewer MCP calls compared to separate pipeline
```

### Error Recovery
```
If submit_full_generation reports failures:
1. Check error_details for specific items
2. Fix the issue (e.g. missing title/link, sparse details)
3. Re-run with just the failed items
4. Idempotent — re-running successful items just updates them
```

---

## 📊 Tool Summary

| # | Tool | Input | Output | Pipeline |
|---|------|-------|--------|----------|
| 1 | get_migration_status | None | Migration progress | Migration |
| 2 | get_contests_for_migration | batch_size, skip | Contests needing patches | Migration |
| 3 | apply_migration_patch | contest_id, patch_json | Single contest updated | Migration |
| 4 | bulk_apply_migrations | migrations_json | Multiple contests updated | Migration |
| **5** | **get_records_for_full_generation** | source, limit, require_validated | Raw records + both prompts | **Full Generation** |
| **6** | **submit_full_generation** | generation_json | Contest + details saved | **Full Generation** |
| **7** | **find_duplicate_contests** | min_live (default 2) | Live duplicate-title groups | **Duplicate Audit** |
| **8** | **get_records_for_events** | source, limit, collection_name | Raw event URLs + events prompt | **Events** |
| **9** | **submit_structured_events** | events_json, dedupe_gate, keep_metadata | Events saved to Events collection | **Events** |
| **10** | **get_events** | event_type, status, upcoming_only, limit, skip | Filtered events | **Events** |
| **11** | **get_events_overview** | None | Events stats (by type/status/upcoming) | **Events** |
| **12** | **get_events_for_detail_generation** | batch_size, skip | Events + event details prompt | **Events Details** |
| **13** | **submit_event_details** | event_id, details_json | Versioned event_details saved | **Events Details** |
| **14** | **get_event_detail_status** | None | Event details coverage metrics | **Events Details** |

**New:** Tools #5 and #6 are the Full Generation Pipeline — raw → published in one pass.
Tools #8–#11 are the **Events Pipeline** — raw event URLs → structured events in the
`Events` collection, readable by AI chatbots via `get_events`.
Tools #12–#14 are the **Events Details Pipeline** — AI-written detail pages
(whyAttend, whoShouldAttend, benefits, tips, agenda highlights, FAQ, SEO) with
versioned storage, mirroring the contest detail generation flow, plus
`get_event_detail_status` to track coverage.

---

## 🛡️ Duplicate-Title Gate (ingestion safety)

All three write tools — `submit_structured_records`, `submit_full_generation` and
`process_raw_data` — accept a **`dedupe_gate`** parameter (default **`true`**).

### What it does

Before inserting, the tool checks every incoming title against all **live**
contests in the collection using a **normalized-title key** (lowercase,
alphanumeric-only, words *sorted* — the exact same normalization as the
backend's `backend/scripts/dedupeContests.js`).

| Incoming record | Gate behavior |
|---|---|
| Same source + **exact same title** | ✅ Passes — the intended re-submit / update path (upserts in place, as before) |
| Same contest from a **different source** | 🚫 Blocked — reported under `duplicates` |
| **Reworded title** from the same source (e.g. word-reordered) | 🚫 Blocked — reported under `duplicates` |
| Repeats a record already in the same batch | 🚫 Blocked — reported under `duplicates` |

Blocked records are **never written** — they are counted in a new `duplicates`
field in the tool response, and each block is explained in `details` /
`error_details` with the existing contest's `_id`, title, source and link.

### Force-insert

Pass **`dedupe_gate=false`** to restore the old behavior (e.g. intentional
re-ingest or a genuine same-title series edition that must be kept separate).

### On-demand audit

`find_duplicate_contests()` is a read-only tool that returns every live
duplicate-title group (with member `_id`s) so the collection can be audited or
cleaned at any time. Archived contests are ignored.

**Verified:** the gate blocks real duplicates with zero writes (tested against
the live ContestHopperDb — 0 existing duplicate groups remain post-dedup).

---

# 🎪 Events Pipeline (participatory events → structured Events collection)

Four tools let AI chatbots harvest, structure, persist, and query events
(conferences, summits, workshops, webinars, meetups, expos, trade shows,
career fairs, networking events, trainings, festivals) in one clean workflow.

---

## 8️⃣ **get_records_for_events**

### Purpose
Fetch raw event URLs/titles + the full events prompt (events-v1.1 schema) so a
chatbot can research and structure participatory events.

### Parameters
- `source` (string, optional) — filter by scraper source field (e.g. `"women_opportunities_aug_2026"`)
- `limit` (integer, 1-25, default 10) — max raw records to fetch
- `collection_name` (string, default `"raw_urls"`) — raw DB collection to read from

### Example Call
```bash
Tool: get_records_for_events
Parameters:
  source: "women_opportunities_aug_2026"
  limit: 10
```

### What's in the Response
- ✅ Raw event records (URLs/titles for the AI to research)
- ✅ `prompt_text` — full events-v1.1 schema & rules (`event-structuring-v1.1.txt`)
- ✅ Usage instructions telling the AI to output ONE event JSON per record

### Next Step
Chatbot structures each event with the prompt → submit via `submit_structured_events`.

---

## 9️⃣ **submit_structured_events**

### Purpose
Persist structured events (events-v1.1 schema) produced by a chatbot into the
**Events** collection. The event counterpart of `submit_structured_records`.

### Parameters
- `events_json` (string) — single event object OR array of event objects
- `dedupe_gate` (boolean, default true) — block duplicates by normalized title
  (same semantics as the contest gate)
- `keep_metadata` (boolean, default false) — retain each event's audit
  `metadata` block (searchLog / fieldConfidence / discoveredEvents) instead of stripping it

### Automatic Normalization
- `type` forced to `"event"`, `slug` auto-generated from title if missing
- Defaults: `status="draft"`, `visibility="public"`, `featured=false`, `analytics` zeros
- Off-schema enum values (`eventType`, `registration.status`, `venue.mode`,
  `difficultyLevel`, `status`) are downgraded to `null` and reported in `warnings`
- Audit `metadata` block stripped unless `keep_metadata=true`
- Dedup key: `source.name + title` (re-submits upsert in place)

### Example Response
```json
{
  "success": true,
  "total_submitted": 3,
  "inserted": 3,
  "updated": 0,
  "duplicates": 0,
  "skipped": 0,
  "errors": 0,
  "event_warnings": 1,
  "details": [],
  "warnings": ["registration.status 'bogus' is invalid; set to null"]
}
```

---

## 🔟 **get_events**

### Purpose
Read structured events back with filters — e.g. all upcoming conferences, or
draft events awaiting review.

### Parameters
- `event_type` (string, optional) — conference / summit / workshop / webinar /
  meetup / expo / trade_show / career_fair / networking_event /
  training_program / festival
- `status` (string, optional) — published / draft / cancelled / archived
- `upcoming_only` (boolean, default false) — only events with `eventDates.start` in the future
- `limit` (integer, 1-100, default 20), `skip` (integer, default 0)

---

## 1️⃣1️⃣ **get_events_overview**

### Purpose
Get counts by `eventType` and `status`, plus upcoming-event count, to decide
what to review or process next.

---

## 🔄 Complete Events Workflow

```
1. Tool: get_events_overview              → see what's already structured
2. Tool: get_records_for_events(source=..., limit=10)
   → 10 raw URLs + events-v1.1 prompt
3. Chatbot researches each URL (speakers, agenda, pricing, venue subpages)
   and outputs ONE event JSON per record
4. Tool: submit_structured_events(events_json)
   → events persisted to the Events collection
5. Tool: get_events(event_type="conference", upcoming_only=true)
   → read back the structured events
6. Tool: get_events_for_detail_generation(batch_size=10)
   → 10 events + event-details-v1.0.txt prompt
7. Chatbot researches each event (speakers, agenda, pricing, venue) and
   outputs event details JSON
8. Tool: submit_event_details(event_id, details_json)
   → versioned event_details saved (quality-validated)
9. Tool: get_event_detail_status
   → coverage metrics (how many events still need detail pages)
```

---

## 1️⃣2️⃣ **get_events_for_detail_generation**

### Purpose
Return events needing AI-generated detail pages, sorted by priority, bundled
with the full `event-details-v1.0.txt` prompt. The event counterpart of
`get_contests_for_detail_generation`.

### Parameters
- `batch_size` (integer, 1-50, default 10), `skip` (integer, default 0)

### Priority Order
upcoming (`eventDates.start` in the future) > published > registration open >
has speakers/agenda > recently added.

---

## 1️⃣3️⃣ **submit_event_details**

### Purpose
Submit AI-generated event details for validation and versioned storage into
the `event_details` collection. The event counterpart of
`submit_contest_details`.

### Parameters
- `event_id` (string) — MongoDB ObjectId of the event
- `details_json` (string) — event-details-v1.0.txt schema JSON

### Automatic Validation
- Rejects truly empty content (< 50 words or no meaningful sections)
- Honest `readingTime` recompute, first-person language check,
  hallucinated-URL check, FAQ structure check, SEO length checks
- Versioned upsert (`event_details` collection) with `previousVersionAt`,
  `changeLog`, and a quality score

### Example Response
```json
{
  "success": true,
  "version": 1,
  "is_new": true,
  "quality_score": 85,
  "validation": { "valid": true, "warning_count": 1, "warnings": ["..."], "total_words": 540 }
}
```

## 1️⃣4️⃣ **get_event_detail_status**

### Purpose
Read-only coverage metrics for the event detail generation pipeline — how many
live events exist, how many already have `event_details`, how many still need
them, the breakdown by detail status, and the coverage percentage. Surfaces
`EventDetailGenerator.get_status()`.

### Parameters
None

### Example Response
```json
{
  "success": true,
  "collection": "event_details",
  "total_events": 100,
  "total_with_details": 58,
  "total_without_details": 42,
  "by_status": { "completed": 58, "failed": 4 },
  "coverage_pct": 58.0
}
```

### When to Use
- **Before generating:** see how much event-detail work remains
- **Between batches:** track coverage % as `submit_event_details` runs
- **Final check:** confirm `coverage_pct` reached 100 (or `total_without_details` is 0)

---

## 🗑️ replace_contest — guarded duplicate delete

### Purpose
Safely remove old-schema duplicates once a new-schema record is in place.
Deletion is refused unless a guard classifies the title group as a safe
replacement — and even then, every duplicate is **archived first**.

### Safety Rules
1. **Dry run by default** — re-run with `dry_run=false` to actually delete
2. **Exact-title only** — every duplicate must have the same title
   (case/punctuation/whitespace-insensitive, same word order). Reworded
   titles → `review_required`, nothing deleted
3. **Archive before delete** — full snapshots go to `<collection>_archived`
   with `replacedBy` + `archivedAt`; incomplete archiving aborts the delete
4. **Reversible** — `restore_contest(archive_id)` puts a doc back under its
   original `_id`

### Example Call
```bash
Tool: replace_contest
Parameters: new_record_id: "65f8a1...", dry_run: true   # preview first
Parameters: new_record_id: "65f8a1...", dry_run: false  # archive + delete
```

### Example Response (live run)
```json
{
  "success": true,
  "verdict": "safe_replace",
  "dry_run": false,
  "kept_id": "65f8a1...",
  "archived_ids": ["65f899..."],
  "deleted": [{ "_id": "65f899...", "deleted": true, "restorable": true }],
  "archive_collection": "Contests_archived",
  "message": "Archived 1 duplicate(s) to Contests_archived, then removed them from Contests. Restore anytime with restore_contest."
}
```

### Verdicts
| Verdict | Meaning | Deletes? |
|---|---|---|
| `safe_replace` | All others in the title group share the exact title | ✅ after archive |
| `review_required` | A reworded title matches only after word-sorting | ❌ |
| `blocked` | No other live record shares the title (or no usable title) | ❌ |

### When to Use
- After `submit_structured_records` inserts a new-schema record for a contest
  that already exists under the old schema
- Together with `find_duplicate_contests` to audit and clean duplicates

---

Prompt files now use descriptive names. The old `Prompts*.txt` names are kept
as alias copies in `prompts/` so nothing breaks.

| Canonical file (referenced by tools) | Alias (kept for compat) | Used by |
|---|---|---|
| `contest-structuring-v4.0.txt` | `Prompts.txt` | get_records_for_structuring, get_records_for_full_generation |
| `contest-details-v1.0.txt` | `Prompts-contest-details.txt` | get_contests_for_detail_generation, submit_contest_details |
| `event-structuring-v1.1.txt` | `Prompts-events.txt` | get_records_for_events |
| `event-details-v1.0.txt` | `Prompts-event-details.txt` | get_events_for_detail_generation, submit_event_details |
| `contest-backfill-v2.0.txt` | `Prompts-backfill.txt` | get_prompted_contests |
| `validation-v1.0.txt` | `Prompts-validation.txt` | get_records_for_validation, get_validation_prompt |

# 🧩 Code Structure (refactored)

The ~3,300-line `main.py` monolith has been split into a proper package:

```
dataflow_mcp/
├── core.py            # FastMCP instance, rate limiter, metrics, prompt loading, normalization helpers
├── server.py          # tool registration + mcp.run()
├── tools/
│   ├── health.py      # health_check (includes MongoDB connectivity)
│   ├── crud.py        # read_collection, get_document, create/update/delete_document
│   ├── migration.py   # get_migration_status, get_prompted_contests, apply/bulk patches
│   ├── contests.py    # structuring + full generation + detail generation
│   ├── events.py      # get_records_for_events, submit_structured_events, get_events, get_events_overview, get_events_for_detail_generation, submit_event_details, get_event_detail_status
│   ├── raw_data.py    # get_scraped_overview, read_raw_collection, process_raw_data
│   ├── validation.py  # claim/submit validation, status, get_workflow_prompt
│   └── audit.py       # find_duplicate_contests, replace_contest, restore_contest, flag_contest_discrepancy, audit_records, get_stale_status_records, refresh_stale_statuses
main.py                # thin entry point → dataflow_mcp.server
```

Run with `python main.py`, `python -m dataflow_mcp.server`, or the installed
`dataflow-mcp` console script. All 41 tools keep their exact names — no client
changes needed.

---

# ⬆️ CustomMCP Server Upgrades (2026-09 spec)

The 2026-09 upgrade spec (11 items) is implemented across the server. Item 4
(connector session stability) is client-side and out of scope for this codebase.

## 1️⃣2️⃣ Write-time enum & schema validation (spec item 1)

Every contest write path can now enforce the canonical enums **before** data
reaches MongoDB. The validator lives in `tools/schema_validation.py` — one
shared set of vocabularies for `update_document`, `create_document`,
`submit_structured_records`, `submit_full_generation`, and the migration patch
validator.

**`update_document` / `create_document`** accept `validate: true` (opt-in
strict mode):

```json
Tool: update_document
Parameters:
  collection_name: "Contests"
  document_id: "6aaea300cb599abddcca0f33"
  update_json: "{\"location\": {\"scope\": \"onsite\"}}"
  validate: true
```

```json
{
  "success": false,
  "error": "Schema validation failed (1 error(s)) — nothing was written",
  "validation": {
    "error_count": 1,
    "notice_count": 0,
    "errors": [
      {
        "field": "location.scope",
        "value": "onsite",
        "reason": "not in canonical enum",
        "severity": "error",
        "expected": "one of: city, country, region, worldwide, online, hybrid, multi_location, unknown"
      }
    ],
    "notices": []
  }
}
```

Enforced: `audience.mode`, `audience.skillLevels`, `audience.primarySkillLevel`,
`audience.skillLevelSource`, `location.scope`, `location.precision`,
`participationGeography.scope`, canonical `allowedRegions` (aliases like "EU"
and sub-national units like "Oklahoma" are flagged with the canonical spelling
in `expected`), restricted-scope-with-empty-lists, fee states, flags, tier
types, and ISO date formats. Legacy categories are notices, not errors.

**`submit_structured_records` / `submit_full_generation`** validate every
record unconditionally — a record with enum violations is skipped and reported
per-record (it is NOT stored).

## 1️⃣3️⃣ `update_document` returns the updated document (spec item 2)

```json
{
  "success": true,
  "modified_count": 1,
  "update_semantics": "deep merge (plain object expanded to dotted $set paths)",
  "changes": { "audience": { "before": {"mode": "online"}, "after": {"mode": "hybrid"} } },
  "document": { "_id": "6aaea...", "audience": {"mode": "hybrid", "eligibilityLabel": "Open to students"} }
}
```

**Documented merge semantics:**
- Plain object → **deep merge**: `{"audience": {"mode": "hybrid"}}` touches
  only `audience.mode`; sibling sub-fields are preserved
- Arrays and `null` → replace wholesale
- Operators `$set` / `$unset` / `$push` / `$pull` → passed through to MongoDB
- Unsupported operators → structured error listing the operator (never a
  generic "Database error occurred")

## 1️⃣4️⃣ `$unset` support (spec item 5)

```json
Tool: update_document
Parameters:
  update_json: "{\"$unset\": {\"timeline.eventEndUTC\": \"\"}}"
```

Removes the field entirely (absent ≠ null for downstream filters). Genuine DB
errors now return `{error, error_code, details, operator_attempted}`.

## 1️⃣5️⃣ Actionable detail warnings (spec item 3)

`submit_contest_details` no longer wastes a submit cycle on readingTime:

- **readingTime** is a non-blocking **notice** — the server computes the
  correct value, stores it, and says so ("stored correctly, no resubmission
  needed"). It never counts against `quality_score`.
- **First-person checker** excludes "US"/"USA" country references and reports
  style issues as notices with the exact rewrite guidance.
- `validation.notices` / `validation.notice_count` appear in every response;
  `quality_score` is driven by warnings only.

## 1️⃣6️⃣ Discoverable pipeline prompts (spec item 6)

```bash
Tool: get_workflow_prompt                    # lists all stages
Tool: get_workflow_prompt
Parameters:
  pipeline_stage: "structuring"              # | validation | detail_generation
                                             # | full_generation | backfill | events
```

Returns the full prompt text plus the designed pipeline for that stage. The
CRUD tool docstrings now route agents: **"When patching existing records,
prefer `get_records_for_*` over raw `read_collection`."**

## 1️⃣7️⃣ Server-side batch audit (spec item 7)

```bash
Tool: audit_records
Parameters:
  collection_name: "Contests"
  ids_json: "[\"6aaea...\"]"        # optional; empty = whole collection
  fields_json: ""                   # optional; default = fee/location/mode/skills/eligibility/geography/deadline
```

```json
{
  "success": true,
  "audited": 120,
  "clean": 113,
  "with_issues": 7,
  "records": [
    { "id": "6aae...", "title": "SeaPerch Challenge", "missing": ["entry.fee.amount"], "empty": [] }
  ],
  "missing_field_counts": { "entry.fee.amount": 4, "audience.mode": 2 }
}
```

Run after every batch — one call instead of N reads plus client-side joins.

## 1️⃣8️⃣ Patch-then-flag flow (spec item 8)

`flag_contest_discrepancy` now accepts structured evidence aliases
(`scrapedValue`/`officialValue` are normalized to
`currentValue`/`observedValue`) and a `resolution` parameter. The docstrings of
`apply_migration_patch`, `update_document`, and the flag tool document the
contract: **when you correct a scraped field, patch AND flag** so validation
reports and DB state stay reconciled.

## 1️⃣9️⃣ Tiered deadlines & fees (spec item 9)

First-class tier block, validated and whitelisted:

```json
"timeline": {
  "tiers": [
    { "type": "early",   "deadlineUTC": "2026-09-30T23:59:59", "entryFee": { "amount": 10, "currency": "EUR" } },
    { "type": "regular", "deadlineUTC": "2026-10-31T23:59:59", "entryFee": { "amount": 15, "currency": "EUR" } },
    { "type": "final",   "deadlineUTC": null,                  "entryFee": { "amount": 120, "currency": "EUR" } }
  ]
}
```

- Structuring prompt **v4.4** (`contest-structuring-v4.4-upgraded.txt`) carries
  the TIERED FEES RULES section; `_build_normalized_record` persists the block
- `apply_migration_patch` accepts `timeline.tiers` (type enum + shape checks)
- Migration of old `entryFees`/`entry`/`feeSummary` variants happens in the
  next restructuring pass via this whitelist entry

## 2️⃣0️⃣ Required edition block (spec item 10)

```json
"edition": {
  "label": "2027",
  "ordinal": 13,
  "cycleStart": "2026-10",
  "cycleEnd": "2027-04"
}
```

- Prompt v4.4 EDITION LOCK RULES make the block required at structuring time;
  dates outside the edition cycle must be treated as other-edition evidence
- The write-time validator checks formats (YYYY-MM cycle bounds, integer
  ordinal, label order) — the machine-checkable half of TARGET IDENTITY LOCK
- Whitelisted for patches as `edition`, `edition.label`, `edition.ordinal`,
  `edition.cycleStart`, `edition.cycleEnd`

## 2️⃣1️⃣ Status freshness guard (spec item 11)

```bash
Tool: get_stale_status_records      # read-only: open/scheduled records past their deadline
Tool: refresh_stale_statuses
Parameters:
  dry_run: true                     # default — preview first
```

`refresh_stale_statuses(dry_run=false)` flips past-due records to `"closed"`
with `statusAutoClosed: true` + `statusCheckedAt` audit notes, so automated
closes are always distinguishable from researched ones. Safe to re-run; the
status filter in the update guard prevents double-writes.

## Test coverage

```
tests/test_schema_validation.py   # spec items 1, 9, 10 — validator behavior
tests/test_detail_warnings.py     # spec items 2, 3 — deep-merge + warning semantics
```

---

# 🧹 Tool Consolidation (2026-09)

Tool count reduced from 48 → **40** with zero functionality loss, plus the
images pipeline removed (handled manually outside the server).

## Merged tools

| Removed tool | Replaced by | Notes |
|---|---|---|
| `get_raw_data_status` | `get_scraped_overview` | Its stats (records with missing critical fields) merged into the overview response as `records_with_missing_fields` |
| `get_contests_for_migration` | `get_prompted_contests` | Call with `include_prompt=false` to get just the documents needing migration |
| `get_validation_prompt` | `get_workflow_prompt("validation")` | One prompt-entry point for every pipeline stage |
| `database_status` | `health_check` | MongoDB connectivity now reported under `health_check.mongodb` |

## Removed tools (no replacement — workflow done manually)

| Removed | What it did |
|---|---|
| `get_contests_missing_images` | List contests missing banner images |
| `get_contests_with_broken_images` | List contests with unreachable image URLs |
| `generate_cover_prompt_for_contest` | AI cover-banner prompt generation |
| `verify_image_urls` | Batch URL status checking |

Also: the `auto_image` parameter was removed from `process_raw_data` — it was
accepted but never actually executed (dead parameter).

**Tip for existing sessions:** if a saved prompt references a removed tool,
the mappings in the table above are drop-in equivalents.

---

# 🔄 Restructure Path (fixing "AI missing fields" on old-schema records)

**The problem this solves:** `get_records_for_structuring` pulls RAW scrapes
from CHRawdata — there was no designed path for restructuring a record already
in the DB. Agents improvised with raw CRUD, never saw the v4.4 checklist, and
the patch whitelist silently dropped well-formed fields. Result: "why is the
AI missing fields?"

## 2️⃣2️⃣ **get_contest_for_restructuring**

Fetches an EXISTING contest + the v4.4 structuring prompt + its specific field
gaps in one call:

```bash
Tool: get_contest_for_restructuring
Parameters:
  contest_id: "6aaea300cb599abddcca0f33"   # preferred
  # OR
  title: "Fine Art Photography Awards"     # exact match, then unique fragment
```

Response contains:
- `contest` — the existing document (fields NOT in `legacy_gaps` are presumed correct)
- `prompt_text` — the full v4.4 structuring checklist
- `legacy_gaps` — `{missing: [...], legacy: [...]}`: exactly what to research
  (missing v4.4 fields) and what to clear (deprecated shapes like the
  `audience.location` string, top-level `prizeSummary`/`feeConfidence`,
  `entryFees` object, ad-hoc tier deadline keys)
- `workflow` — the 6-step patch → flag → audit flow

### Recommended restructure flow

```
get_contest_for_restructuring(contest_id=…)
  → research gaps against the official source (web search)
  → apply_migration_patch(contest_id, patch_json)        # only gap fields
      (force=true ONLY when nulling a populated legacy field)
  → flag_contest_discrepancy(…)                          # if a scraped value was corrected
  → audit_records(ids_json=["<contest_id>"])             # must report clean
```

## Patch whitelist now covers the full v4.4 schema

These fields were previously **silently dropped** from patches (buried warning,
easy to miss) — a perfectly-restructured record could not be fully written:

- `flags`, `audienceScope`
- `source`, `source.name`, `source.url`, `source.type` (enum-checked)
- `filterKeys`, `filterKeys.domain/format/medium/themes`
- `timeline.organizerTimeZone`

…on top of the earlier additions (`timeline.tiers`, `edition.*`). Schema
checks were added for each: invalid `source.type`/`audienceScope`/`flags`
values are rejected with the allowed vocabulary in the error.

**Note for re-submitters:** the duplicate-title gate still applies to
`submit_structured_records` — for records already in the DB, use the
restructure flow above rather than resubmitting the full record.

---

# 🗂️ Canonical Category / Subcategory Taxonomy (spec item 12)

**The problem this solves:** agents invented category names ("Robotics &
Autonomous Systems", "Illustration & Visual Art") and arbitrary subcategories,
producing a fragmented taxonomy that breaks filtering and dedup.

## 2️⃣3️⃣ **get_taxonomy**

Returns the canonical vocabulary the server accepts. Taxonomy is **code**
(`tools/taxonomy.py`), sourced from the client's own category doc — it is never
mutable through record writes.

```bash
Tool: get_taxonomy                    # all 10 categories + every subcategory
Tool: get_taxonomy
Parameters:
  category: "Engineering & Innovation"  # one category's full subcategory list
```

Unknown category → structured error + hint to call `get_taxonomy()`.

### The 10 canonical categories

1. AI & Technology
2. Engineering & Innovation
3. Business & Entrepreneurship
4. Science & Research
5. Creative Arts & Design
6. Writing & Media
7. Environment & Sustainability
8. Education & Learning
9. Social Impact & Leadership
10. Open & Multidisciplinary

(`Engineering Design` IS canonical — the spec's item-12 example was a
*specificity* misuse, not an invalid category.)

### Resolution rules (applied on ingestion and on patch)

| Incoming value | Result |
| --- | --- |
| Exact canonical category/subcategory | accepted |
| Case-only difference (`"ai & technology"`) | normalized to canonical + **notice** |
| Known alias (`Technology & AI` → `AI & Technology`) | auto-mapped |
| Ambiguous invention (no safe 1:1 mapping) | `status="ambiguous"` — flagged, **never guessed** |
| Unknown value | **error** with a `get_taxonomy` hint |

- `_check_category_pair` in `tools/schema_validation.py` validates a
  subcategory against its parent category — and, when only `subCategory` is
  patched, against the union of all canonical subcategories as a fallback.
- `_build_normalized_record` in `dataflow_mcp/core.py` auto-maps
  category/subcategory on ingestion (logging every change) and **drops** any
  non-canonical subCategory rather than storing an invention.
- The v4.4 structuring prompt's CATEGORY RULES block delegates to
  `get_taxonomy` instead of restating the list.

### Contract

- `get_taxonomy` is **read-only** — the taxonomy is not extensible via any
  write tool. Adding a category/subcategory requires a code change.
- Ambiguous values are surfaced for human review; the server never invents a
  mapping to make a write succeed.
