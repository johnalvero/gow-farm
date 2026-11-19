# 🎮 344 Accounts Setup - Important Information

## ✅ Conversion Complete!

Your **344 accounts** from `farms.csv` have been successfully converted to `users.txt`!

---

## 📊 Account Summary

| Source | Accounts | Status |
|--------|----------|--------|
| farms.csv | 344 | ✅ Converted |
| users.txt | 344 | ✅ Ready to use |

**Format:** `email:password` (one per line)

---

## ⚠️ IMPORTANT: Processing Time

With **344 accounts**, claiming jewels will take a significant amount of time:

| Metric | Time |
|--------|------|
| **Per account** | ~20 seconds |
| **Wait between accounts** | 10 seconds |
| **Total time** | ~3 hours! |

**Breakdown:**
- 344 accounts × 20 seconds = 6,880 seconds (114 minutes)
- 343 waits × 10 seconds = 3,430 seconds (57 minutes)
- **Total: ~171 minutes (2.8 hours)**

---

## 💎 Jewel Earnings

| Period | Total Jewels |
|--------|--------------|
| **Daily** | 25,800 jewels (344 × 75) |
| **Weekly** | 180,600 jewels |
| **Monthly** | 774,000 jewels |
| **Yearly** | 9,417,000 jewels! |

---

## 🚨 Recommendations

### Option 1: Split Into Batches (Recommended)

Instead of running all 344 at once, split them into batches:

**Create batch files:**
```bash
# Batch 1 (accounts 1-50)
head -50 users.txt > users_batch1.txt

# Batch 2 (accounts 51-100)
sed -n '51,100p' users.txt > users_batch2.txt

# Batch 3 (accounts 101-150)
sed -n '101,150p' users.txt > users_batch3.txt

# And so on...
```

**Schedule different batches at different times:**
```
Batch 1: 1:00 AM (50 accounts = ~25 minutes)
Batch 2: 2:00 AM (50 accounts = ~25 minutes)
Batch 3: 3:00 AM (50 accounts = ~25 minutes)
Batch 4: 4:00 AM (50 accounts = ~25 minutes)
Batch 5: 5:00 AM (50 accounts = ~25 minutes)
Batch 6: 6:00 AM (50 accounts = ~25 minutes)
Batch 7: 7:00 AM (44 accounts = ~22 minutes)
```

**Benefits:**
- ✅ Spreads out load
- ✅ Less likely to trigger rate limiting
- ✅ Easier to debug issues
- ✅ Can monitor progress throughout the day

### Option 2: Reduce Wait Time

Edit `claim_all.py` and reduce the wait between accounts:

```python
# Line ~133 (in the main() function)
# Change from:
await asyncio.sleep(10)  # 10 seconds

# To:
await asyncio.sleep(3)  # 3 seconds
```

**New total time:** ~2 hours instead of 3 hours

**Trade-off:** Higher risk of rate limiting

### Option 3: Run All at Once (Not Recommended)

If you want to run all 344 accounts in one go:

```bash
.venv/bin/python claim_all.py
```

**Be prepared to wait 3 hours!**

---

## 🧪 Test First!

Before running all 344 accounts, test with a small batch:

```bash
# Test with 3 accounts
head -3 users.txt > users_test.txt

# Temporarily modify claim_all.py to use users_test.txt
# Or create a copy of claim_all.py for testing

.venv/bin/python claim_all.py
```

**If test succeeds:** Proceed with full run or batches

---

## 📁 Batch Processing Script

I'll create a batch processing script for you:

**`claim_batch.py`** - Processes a range of accounts

Usage:
```bash
# Process accounts 1-50
.venv/bin/python claim_batch.py 1 50

# Process accounts 51-100
.venv/bin/python claim_batch.py 51 100
```

---

## ⏰ Suggested Schedule (7 Batches)

### Batch 1 (1-50): 1:00 AM
```bash
0 1 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 1 50 >> batch1.log 2>&1
```

### Batch 2 (51-100): 2:00 AM
```bash
0 2 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 51 100 >> batch2.log 2>&1
```

### Batch 3 (101-150): 3:00 AM
```bash
0 3 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 101 150 >> batch3.log 2>&1
```

### Batch 4 (151-200): 4:00 AM
```bash
0 4 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 151 200 >> batch4.log 2>&1
```

### Batch 5 (201-250): 5:00 AM
```bash
0 5 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 201 250 >> batch5.log 2>&1
```

### Batch 6 (251-300): 6:00 AM
```bash
0 6 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 251 300 >> batch6.log 2>&1
```

### Batch 7 (301-344): 7:00 AM
```bash
0 7 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_batch.py 301 344 >> batch7.log 2>&1
```

---

## 🔍 Monitoring

### Check Progress
```bash
# View batch 1 log
tail -f batch1.log

# Count successes in batch 1
grep "✅ SUCCESS" batch1.log | wc -l

# View all batch summaries
for i in {1..7}; do
  echo "=== Batch $i ==="
  grep "Total:" batch$i.log | tail -1
done
```

---

## ⚠️ Rate Limiting Concerns

With 344 accounts, you might encounter:

1. **IP Rate Limiting**
   - Game of War might block your IP temporarily
   - Solution: Use batches spread over several hours

2. **Account Flagging**
   - Multiple accounts from same IP might look suspicious
   - Solution: Use different delays, batches at different times

3. **Connection Timeouts**
   - Long-running processes might lose connection
   - Solution: Use batches, restart if needed

---

## 💡 Best Practices

1. **Start Small**
   - Test with 10-20 accounts first
   - Monitor for issues
   - Scale up gradually

2. **Use Batches**
   - 50 accounts per batch is a good balance
   - Spread across 6-7 hours

3. **Monitor Logs**
   - Check daily for failures
   - Investigate errors promptly

4. **Backup users.txt**
   ```bash
   cp users.txt users.txt.backup
   ```

5. **Keep Screenshots**
   - Evidence of successful claims
   - Useful for debugging

---

## 📈 Scaling Strategy

### Week 1: Test Phase
- Run 10 accounts manually
- Verify all succeed
- Check for bans/blocks

### Week 2: Small Batch
- Run 50 accounts (1 batch)
- Monitor closely
- Adjust timing if needed

### Week 3: Multiple Batches
- Run 2-3 batches (100-150 accounts)
- Spread across morning hours
- Check success rate

### Week 4+: Full Scale
- Run all 7 batches (344 accounts)
- Automated daily
- Monitor weekly

---

## 🎯 Decision Matrix

| Your Goal | Recommended Approach |
|-----------|---------------------|
| **Maximum jewels** | All 344 accounts, batched over 7 hours |
| **Low risk** | Start with 50 accounts, expand gradually |
| **Fast testing** | Run 10 accounts manually |
| **Set and forget** | 7 batches, automated via cron |

---

## 📝 Next Steps

1. **Choose your approach:**
   - [ ] All at once (3 hours)
   - [ ] Batches (7 batches × 25 min each)
   - [ ] Test first (10-50 accounts)

2. **Create batch script** (if batching)
3. **Set up cron jobs** (if automating)
4. **Test with small batch**
5. **Monitor first run**
6. **Scale up gradually**

---

## ⚡ Quick Start (Batched Approach)

I'll create the batch processing script next. You can then:

1. Test: `.venv/bin/python claim_batch.py 1 10`
2. If successful, set up all 7 cron jobs
3. Check logs tomorrow

**This is the safest and most reliable approach for 344 accounts!**
