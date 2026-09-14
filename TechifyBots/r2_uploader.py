"""
Cloudflare R2 upload helper for the Reels WebApp feature.

R2 is S3-API-compatible, so we use aioboto3 (async boto3) with R2's endpoint.
This module is intentionally self-contained and never raises out of its public
functions — indexing must keep working even if R2 is unreachable/misconfigured.
"""

import aioboto3
from botocore.config import Config as BotoConfig
from vars import (
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
    R2_BUCKET_NAME, R2_ENABLED,
)

_R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""

_session = aioboto3.Session()


def _client_ctx():
    """Return an async-context-manager R2 client. Caller must `async with` it."""
    return _session.client(
        "s3",
        endpoint_url=_R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
    )


async def upload_file(local_path: str, key: str) -> int:
    """
    Upload a local file to the R2 bucket under `key`.
    Returns the uploaded file size in bytes on success, or -1 on failure.
    Never raises — callers treat -1 as "mirror failed, try again later".
    """
    if not R2_ENABLED:
        return -1
    import os
    try:
        size = os.path.getsize(local_path)
    except OSError as e:
        print(f"[r2_uploader] stat failed for {local_path}: {e}")
        return -1

    try:
        async with _client_ctx() as s3:
            with open(local_path, "rb") as f:
                await s3.upload_fileobj(
                    f, R2_BUCKET_NAME, key,
                    ExtraArgs={"ContentType": "video/mp4"},
                )
        return size
    except Exception as e:
        print(f"[r2_uploader] upload failed for key={key}: {e}")
        return -1


async def delete_file(key: str) -> bool:
    """Delete an object from the R2 bucket. Returns True on success."""
    if not R2_ENABLED:
        return False
    try:
        async with _client_ctx() as s3:
            await s3.delete_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except Exception as e:
        print(f"[r2_uploader] delete failed for key={key}: {e}")
        return False
