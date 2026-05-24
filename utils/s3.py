import logging
import os

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger("docai.utils.s3")

# Pull S3 configuration from environment variables
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL")  # Crucial for Cloudflare R2 / Backblaze B2


def is_s3_enabled() -> bool:
    """Check if AWS S3 (or S3-compatible) credentials are fully configured."""
    return bool(AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_STORAGE_BUCKET_NAME)


def _get_s3_client():
    """Build and return a boto3 S3 client using environment config."""
    if not is_s3_enabled():
        return None

    client_kwargs = {
        "aws_access_key_id": AWS_ACCESS_KEY_ID,
        "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
        "config": Config(signature_version="s3v4"),
    }

    if AWS_S3_REGION_NAME:
        client_kwargs["region_name"] = AWS_S3_REGION_NAME
    if AWS_S3_ENDPOINT_URL:
        client_kwargs["endpoint_url"] = AWS_S3_ENDPOINT_URL

    return boto3.client("s3", **client_kwargs)


def upload_file_bytes(file_bytes: bytes, key: str, content_type: str = "application/pdf") -> str | None:
    """
    Upload raw bytes to the configured S3 bucket.
    Returns the s3:// URI (e.g., s3://my-bucket/filename.pdf) on success.
    """
    s3_client = _get_s3_client()
    if not s3_client:
        logger.warning("S3 client is not enabled. Skipping upload.")
        return None

    try:
        logger.info("Uploading %d bytes to S3 bucket %s with key %s...", len(file_bytes), AWS_STORAGE_BUCKET_NAME, key)
        s3_client.put_object(
            Bucket=AWS_STORAGE_BUCKET_NAME,
            Key=key,
            Body=file_bytes,
            ContentType=content_type,
        )
        s3_uri = f"s3://{AWS_STORAGE_BUCKET_NAME}/{key}"
        logger.info("S3 upload successful: %s", s3_uri)
        return s3_uri
    except ClientError as e:
        logger.error("Failed to upload bytes to S3: %s", e)
        raise OSError(f"S3 upload error: {e}") from e


def download_file_bytes(key: str) -> bytes:
    """Download and return file bytes from the S3 bucket using the provided key."""
    s3_client = _get_s3_client()
    if not s3_client:
        raise ValueError("S3 client is not enabled. Cannot download file.")

    try:
        logger.info("Downloading file with key %s from S3 bucket %s...", key, AWS_STORAGE_BUCKET_NAME)
        response = s3_client.get_object(Bucket=AWS_STORAGE_BUCKET_NAME, Key=key)
        return response["Body"].read()
    except ClientError as e:
        logger.error("Failed to download file from S3: %s", e)
        raise FileNotFoundError(f"S3 key {key!r} not found: {e}") from e


def delete_file(key: str) -> bool:
    """Delete a file from the S3 bucket."""
    s3_client = _get_s3_client()
    if not s3_client:
        logger.warning("S3 client is not enabled. Skipping deletion.")
        return False

    try:
        logger.info("Deleting key %s from S3 bucket %s...", key, AWS_STORAGE_BUCKET_NAME)
        s3_client.delete_object(Bucket=AWS_STORAGE_BUCKET_NAME, Key=key)
        logger.info("S3 key %s deleted successfully.", key)
        return True
    except ClientError as e:
        logger.error("Failed to delete key %s from S3: %s", key, e)
        return False
