#!/usr/bin/env python3
"""Upload the generated demo corpus to S3 under ``$S3_PREFIX``.

Reads env vars:
    S3_BUCKET_NAME (required)
    S3_PREFIX      (default: "demo/")
    AWS_REGION     (optional)

Run:    python demo/seed_corpus.py
"""
from __future__ import annotations

import mimetypes
import os
import sys
from pathlib import Path

import boto3

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"


def main() -> int:
    bucket = os.environ.get("S3_BUCKET_NAME")
    if not bucket:
        print("S3_BUCKET_NAME is required", file=sys.stderr)
        return 2

    prefix = os.environ.get("S3_PREFIX", "demo/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    if not CORPUS.exists():
        print(f"No corpus at {CORPUS}. Run build_corpus.py first.", file=sys.stderr)
        return 2

    region = os.environ.get("AWS_REGION")
    s3 = boto3.client("s3", region_name=region) if region else boto3.client("s3")

    uploaded = 0
    for path in CORPUS.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(CORPUS).as_posix()
        key = f"{prefix}{rel}"
        ctype, _ = mimetypes.guess_type(path.name)
        extra = {"ContentType": ctype} if ctype else {}
        s3.upload_file(str(path), bucket, key, ExtraArgs=extra)
        uploaded += 1
        print(f"  s3://{bucket}/{key}")

    print(f"\nUploaded {uploaded} files to s3://{bucket}/{prefix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
