"""
Cloudflare R2 multi-account configuration — lives in the repo, not in Koyeb's
environment variables, by choice.

HOW TO ADD MORE STORAGE (no other code, anywhere, needs to change):
  1. Copy one whole "ACCOUNT" block below (all its lines).
  2. Paste it right after the last account block.
  3. In the pasted copy, change every "_N" in the variable names to the next
     number (e.g. every "_2" becomes "_3").
  4. Fill in that account's real values from Cloudflare.
  5. Redeploy.

The bot automatically detects how many numbered accounts are filled in here
and uses all of them, in order (api1 first, then api2, then api3, ...).
Leaving a later block's lines commented out (or deleted) is how you tell the
bot "stop here" — it stops at the first missing number.

Where to find each value in Cloudflare's dashboard:
  - R2_ACCOUNT_ID        → R2 Object Storage → Overview → "Account details"
  - R2_ACCESS_KEY_ID_ /
    R2_SECRET_ACCESS_KEY  → same Overview page → "Manage R2 API Tokens" →
                             Create API token (permission: Object Read & Write,
                             scoped to just this bucket) — shown once, copy both
  - R2_BUCKET_NAME        → the bucket's name, exactly as shown, case-sensitive
  - R2_PUBLIC_BASE_URL    → that bucket → Settings → Public Development URL
                             (or your own custom domain if you've set one up)
  - R2_JURISDICTION       → only needed if that bucket's "Applied to" / location
                             shows a data-residency jurisdiction like "US" —
                             leave "" for a normal/default bucket
  - R2_FREE_STORAGE_GB    → Cloudflare's free tier is 10GB per account; leave
                             at 10 unless you're on a paid plan with more
"""

# ============================== ACCOUNT 1 ==============================
R2_ACCOUNT_ID_1 = "444fb8381f5432bc3ae6a0123b53e4d4"
R2_ACCESS_KEY_ID_1 = ""          # paste your current Access Key ID here
R2_SECRET_ACCESS_KEY_1 = ""      # paste your current Secret Access Key here
R2_BUCKET_NAME_1 = "reels-videos"
R2_PUBLIC_BASE_URL_1 = "https://pub-babd88c1825d4f4c9bb30bcf13f8aa62.r2.dev"
R2_JURISDICTION_1 = "us"
R2_FREE_STORAGE_GB_1 = 10

# ============================== ACCOUNT 2 ==============================
# To add a second account: delete the leading "# " from each line below and
# fill in the real values.
# R2_ACCOUNT_ID_2 = ""
# R2_ACCESS_KEY_ID_2 = ""
# R2_SECRET_ACCESS_KEY_2 = ""
# R2_BUCKET_NAME_2 = ""
# R2_PUBLIC_BASE_URL_2 = ""
# R2_JURISDICTION_2 = ""
# R2_FREE_STORAGE_GB_2 = 10

# ============================== ACCOUNT 3 ==============================
# (copy ACCOUNT 2's 7 lines again, change every "_2" to "_3", uncomment, fill in)


# ── Nothing below this line needs to be touched — this just reads whichever
#    numbered accounts you've filled in above and builds the list the rest
#    of the bot uses. ──────────────────────────────────────────────────────
def _collect_accounts():
    import sys
    module = sys.modules[__name__]
    accounts = []
    idx = 1
    while idx <= 20:  # sane upper bound, not a real limit anyone should hit
        acc_id = getattr(module, f"R2_ACCOUNT_ID_{idx}", None)
        if not acc_id:
            break  # first missing number = stop looking for more
        access_key = getattr(module, f"R2_ACCESS_KEY_ID_{idx}", "")
        secret_key = getattr(module, f"R2_SECRET_ACCESS_KEY_{idx}", "")
        bucket = getattr(module, f"R2_BUCKET_NAME_{idx}", "")
        public_url = (getattr(module, f"R2_PUBLIC_BASE_URL_{idx}", "") or "").rstrip("/")
        if access_key and secret_key and bucket and public_url:
            accounts.append({
                "id": f"api{idx}",
                "account_id": acc_id,
                "access_key_id": access_key,
                "secret_access_key": secret_key,
                "bucket_name": bucket,
                "public_base_url": public_url,
                "jurisdiction": (getattr(module, f"R2_JURISDICTION_{idx}", "") or "").strip().lower(),
                "free_storage_gb": float(getattr(module, f"R2_FREE_STORAGE_GB_{idx}", 10)),
            })
        idx += 1
    return accounts


R2_ACCOUNTS = _collect_accounts()
