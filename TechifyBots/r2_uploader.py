import os

import aioboto3
from botocore.config import Config as BotoConfig
from vars import R2_ACCOUNTS, R2_ENABLED

_session = aioboto3.Session()


def _endpoint_for(account: dict) -> str:
    if account.get("jurisdiction"):
        return f"https://{account['account_id']}.{account['jurisdiction']}.r2.cloudflarestorage.com"
    return f"https://{account['account_id']}.r2.cloudflarestorage.com"


def _client_ctx(account: dict):
    """Return an async-context-manager R2 client for one specific account.
    Caller must `async with` it."""
    return _session.client(
        "s3",
        endpoint_url=_endpoint_for(account),
        aws_access_key_id=account["access_key_id"],
        aws_secret_access_key=account["secret_access_key"],
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )


async def pick_account_with_room() -> dict:
    """
    Returns the first configured account (in api1, api2, api3... order) that
    still has headroom under its own free_storage_gb, or None if every
    configured account is full. This is what makes the pool "automatic" —
    callers never need to know which account they're writing to.
    """
    from Database.maindb import mdb
    for account in R2_ACCOUNTS:
        usage = await mdb.get_r2_usage(account["id"])
        limit_bytes = account["free_storage_gb"] * (1024 ** 3)
        if usage["total_bytes"] < limit_bytes:
            return account
    return None


async def upload_file(account: dict, local_path: str, key: str, content_type: str = "video/mp4") -> int:
    """
    Upload a local file to the given account's bucket under `key`.
    Returns the uploaded file size in bytes on success, or -1 on failure.
    Never raises — callers treat -1 as "mirror failed, try again later".
    """
    if not R2_ENABLED or not account:
        return -1
    try:
        size = os.path.getsize(local_path)
    except OSError as e:
        print(f"[r2_uploader] stat failed for {local_path}: {e}")
        return -1

    try:
        async with _client_ctx(account) as s3:
            with open(local_path, "rb") as f:
                await s3.upload_fileobj(
                    f, account["bucket_name"], key,
                    ExtraArgs={
                        "ContentType": content_type,
                        # These files are content-addressed by message ID and never
                        # change once mirrored — safe to cache at Cloudflare's edge
                        # and in the browser for a long time. This is what makes the
                        # 2nd+ view of any reel near-instant.
                        "CacheControl": "public, max-age=31536000, immutable",
                    },
                )
        return size
    except Exception as e:
        print(f"[r2_uploader] upload failed for key={key} on {account['id']}: {e}")
        return -1


async def delete_file(account: dict, key: str) -> bool:
    """Delete an object from the given account's bucket. Returns True on success."""
    if not R2_ENABLED or not account:
        return False
    try:
        async with _client_ctx(account) as s3:
            await s3.delete_object(Bucket=account["bucket_name"], Key=key)
        return True
    except Exception as e:
        print(f"[r2_uploader] delete failed for key={key} on {account['id']}: {e}")
        return False


async def test_connection(account: dict) -> tuple:
    """Lightweight credential/permission check for /r2check, for ONE account.
    Tests the actual operations the upload path uses (PutObject/GetObject/
    DeleteObject) rather than HeadBucket — R2 tokens scoped to "Object Read &
    Write" often don't include bucket-level permissions, only object-level
    ones, so a HeadBucket check can fail even when real uploads would
    succeed. Returns (ok: bool, detail: str)."""
    if not account:
        return False, "Not configured."
    test_key = "_r2check_test.txt"
    try:
        async with _client_ctx(account) as s3:
            await s3.put_object(Bucket=account["bucket_name"], Key=test_key, Body=b"ok")
            await s3.get_object(Bucket=account["bucket_name"], Key=test_key)
            await s3.delete_object(Bucket=account["bucket_name"], Key=test_key)
        return True, "Write + read + delete all succeeded."
    except Exception as e:
        code = ""
        if hasattr(e, "response"):
            code = e.response.get("Error", {}).get("Code", "") or str(e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
        return False, f"{code or type(e).__name__}: {e}"
