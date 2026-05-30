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
