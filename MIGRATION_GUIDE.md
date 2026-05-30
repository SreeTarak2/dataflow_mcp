# Contest Migration Guide (810 Contests → v4.0 Schema)

## Overview

You have **810 existing contests** in the database using an older schema version. This guide explains how to migrate them to the **v4.0 schema** using the MCP server's migration tools.

## Migration Strategy

### Two-Prompt Approach

1. **Prompts.txt (v4.0)** — For NEW scraped contests
   - Full access to source webpage
   - Complete normalization with all fields
   
2. **Prompts-backfill.txt (v2.0)** — For EXISTING 810 contests
   - No source page access (work with existing data only)
   - Output is a **patch/diff** (only changed fields)
   - Preserves structural fields already set correctly

### Key Difference: Patch vs. Full Document

**New contests (Prompts.txt)** → Output full JSON:
```json
{
  "title": "...",
  "canonicalCategory": "...",
  "prizeSummary": "...",
  ...all fields...
}
```

**Existing contests (Prompts-backfill.txt)** → Output patch only:
```json
{
  "canonicalCategory": "Technology & AI",
  "prizeSummary": "Cash prizes totaling €20,000",
  "tags": ["oscar-qualifying"]
}
```

The patch approach means:
- ✅ Faster processing (AI only enhances missing fields)
- ✅ Preserves existing structural data (dates, links, images set correctly)
- ✅ Lower risk of corrupting data

## Migration Workflow

### Step 1: Check Migration Status

**Tool:** `get_migration_status`

Shows how many of 810 contests still need updates.

```bash
# No parameters needed
```

**Response Example:**
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

### Step 2: Fetch Batch of Unmigrated Contests

**Tool:** `get_contests_for_migration`

Get a batch of contests needing migration.

**Parameters:**
- `batch_size` (default: 10, max: 100)
- `skip` (default: 0) — for pagination

**Response Example:**
```json
{
  "success": true,
  "contests": [
    {
      "_id": "65f8a1b2c3d4e5f6g7h8i9j0",
      "title": "45th Uppsala Short Film Festival",
      "category": "Filmmaking",
      "description": "Sweden's premier short film arena...",
      "prize": {"isMonetary": true, "originalAmount": "€20,000", ...},
      "entry": {"isFree": false, "fee": {"amount": 0, ...}, ...},
      "audience": {"skillLevels": ["beginner", "intermediate", "advanced"], ...}
      ...other fields...
    },
    ...more contests...
  ],
  "count": 10,
  "total_needing_migration": 360,
  "skip": 0,
  "batch_size": 10
}
```

### Step 3: Normalize Contests Using Backfill Prompt

For each contest in the batch, use Claude with the **Prompts-backfill.txt** prompt:

**Claude Prompt Template:**
```
[Copy full Prompts-backfill.txt content here]

Input: [Paste full contest JSON from get_contests_for_migration]

Output: Provide ONLY the patch with fields that need updating.
```

**Example Claude Input (from Step 2):**
```json
{
  "_id": "65f8a1b2c3d4e5f6g7h8i9j0",
  "title": "45th Uppsala Short Film Festival",
  "category": "Filmmaking",
  ...full contest...
}
```

**Example Claude Output (patch):**
```json
{
  "rawCategory": "Filmmaking",
  "canonicalCategory": "Writing & Media",
  "description": "Submit short films under 40 minutes across any genre. Open to filmmakers worldwide. Oscar-qualifying with €20,000 in prizes distributed across three categories.",
  "tags": ["oscar-qualifying"],
  "prize": {
    "prizeSummary": "Cash prizes totaling €20,000"
  },
  "entry": {
    "feeConfidence": "unknown"
  },
  "audience": {
    "primarySkillLevel": "open"
  }
}
```

### Step 4: Apply Patch to Contest

**Tool:** `apply_migration_patch`

Update a single contest with the patch.

**Parameters:**
- `contest_id` (string) — MongoDB ObjectId from Step 2
- `patch_json` (string) — JSON from Claude output in Step 3

**Example Usage:**
```bash
contest_id: "65f8a1b2c3d4e5f6g7h8i9j0"
patch_json: '{"canonicalCategory": "Writing & Media", "prize": {"prizeSummary": "Cash prizes totaling €20,000"}, ...}'
```

**Response:**
```json
{
  "success": true,
  "contest_id": "65f8a1b2c3d4e5f6g7h8i9j0",
  "modified_count": 4,
  "message": "Patch applied successfully"
}
```

### Step 5: Process Additional Batches or Use Bulk

**Option A: Single-by-single**
- Repeat Steps 2-4 with `skip` parameter

**Option B: Bulk Operations (Faster)**
- After getting 10+ patches from Claude, use `bulk_apply_migrations`

**Tool:** `bulk_apply_migrations`

Apply multiple patches at once.

**Parameters:**
- `migrations_json` (string) — Array of {contest_id, patch} objects

**Example:**
```bash
migrations_json: '[
  {"contest_id": "id1", "patch": {...}},
  {"contest_id": "id2", "patch": {...}},
  {"contest_id": "id3", "patch": {...}}
]'
```

**Response:**
```json
{
  "success": true,
  "total": 3,
  "successful": 3,
  "failed": 0,
  "details": [
    {"contest_id": "id1", "status": "success"},
    {"contest_id": "id2", "status": "success"},
    {"contest_id": "id3", "status": "success"}
  ]
}
```

## Complete Workflow Example

### 1. Check Status
```
Tool: get_migration_status
→ Shows 200 contests still need canonicalCategory, 180 need prizeSummary
```

### 2. Get First Batch (10 contests)
```
Tool: get_contests_for_migration
Parameters: batch_size=10, skip=0
→ Returns 10 contest JSON objects
```

### 3. For Each Contest (or batch of 10)
```
Claude Input: Full contest JSON + Prompts-backfill.txt
Claude Output: Patch JSON (only fields to update)
```

### 4a. Single Updates
```
For each patch:
Tool: apply_migration_patch
Parameters: contest_id, patch_json
→ Updates database
```

### 4b. Bulk Updates (Faster)
```
Collect 10 patches:
Tool: bulk_apply_migrations
Parameters: Array of {contest_id, patch} objects
→ Updates database in batch
```

### 5. Pagination
```
Repeat Steps 2-4:
Tool: get_contests_for_migration
Parameters: batch_size=10, skip=10 (then skip=20, skip=30, etc.)
→ Process all 810 contests
```

## Performance Optimization

### Batch Processing Strategy

| Scenario | Tool | Batch Size | Speed |
|----------|------|-----------|-------|
| **Manual review** | Single patches | 1-5 | Slow (~5 min per) |
| **Balanced** | Bulk every 10 | 10 | Medium (~30 min per 10) |
| **Full speed** | Bulk every 50 | 50 | Fast (~2 min per 50) |
| **Max throughput** | Bulk every 100 | 100 | Fastest (~1 min per 100) |

### Recommended Flow for 810 Contests

**Total time: ~3-4 hours with bulk processing**

1. Get status → 200 need updates
2. Process in batches of 50 (~4 hours total)
   - Fetch 50 contests
   - Normalize with Claude (accepts full JSON list)
   - Bulk apply 50 patches
   - Repeat

## What Fields Get Updated (Backfill Only)

The backfill prompt keeps these fields UNCHANGED:
- ✅ type, title, link, image, source (preserve carefully set values)
- ✅ timeline (dates already extracted with source access)
- ✅ filterKeys.domain, format, medium, themes

The backfill prompt ADDS/UPDATES these:
- ✅ `canonicalCategory` — mapped from existing category
- ✅ `rawCategory` — copy of existing category (if missing)
- ✅ `prizeSummary` — humanized prize description
- ✅ `feeConfidence` — confidence level in fee extraction
- ✅ `eligibilityLabel` — humanized eligibility text
- ✅ `description` — rewritten to follow 3-sentence structure
- ✅ `tags[]` — normalized and de-duplicated
- ✅ `primarySkillLevel` — can upgrade to "open" if appropriate

## Migration Safety

### Atomic Updates
- Each patch is applied with `$set` operator
- Existing data not in patch is untouched
- Failed updates don't affect other documents

### Rollback (if needed)
```javascript
// Revert _migration_updated_at to find recently changed docs
db.Contests.find({"_migration_updated_at": {"$gte": ISODate("...")}})

// Roll back specific fields
db.Contests.updateOne(
  {"_id": ObjectId("...")},
  {"$unset": {"canonicalCategory": "", "prizeSummary": ""}}
)
```

### Verification
After migration, check:
```bash
# Verify all contests have v4.0 fields
Tool: get_migration_status
→ Should show 810/810 migrated (100%)
```

## Prompt Files Location

Both prompts are stored in the MCP server:
- `prompts/Prompts.txt` — v4.0 schema (new scraped data)
- `prompts/Prompts-backfill.txt` — v2.0 patch schema (existing data)

## Estimated Effort

- **Manual (Claude one-by-one):** 8-10 hours
- **Bulk (10 at a time):** 4-5 hours
- **Maximum bulk (50 at a time):** 2-3 hours

Each MCP migration tool call takes ~1 sec.
Most time is spent on Claude normalization.

## Troubleshooting

### "Contest not found" Error
- Verify contest_id is valid MongoDB ObjectId string
- Ensure contest exists in database
- Check format: "65f8a1b2c3d4e5f6g7h8i9j0" (24 hex chars)

### Patch Rejected
- Ensure patch_json is valid JSON string
- Patch should only contain fields to update (not full document)
- Check for syntax errors in JSON

### Rate Limit Exceeded
- Wait 60 seconds (default: 100 req/min)
- Or batch larger operations (50 contests at once)

### "No changes needed" Response
- Contest may already be fully migrated
- Run `get_migration_status` to verify overall progress

---

**Next Steps:**
1. Run `get_migration_status` to see current state
2. Process first batch with `get_contests_for_migration`
3. Normalize with Claude using Prompts-backfill.txt
4. Apply patches with `apply_migration_patch` or `bulk_apply_migrations`
