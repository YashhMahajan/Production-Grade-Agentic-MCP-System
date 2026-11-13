"""Create the S3 bucket the server writes to. Safe to run repeatedly.

Runs once inside the scout-mcp image (it already has boto3), so no extra image has
to be pulled and pinned. The S3 gateway can take a few seconds after its container
reports healthy, so this retries for up to a minute.
"""

import os
import sys
import time

import boto3
from botocore.exceptions import BotoCoreError, ClientError

endpoint = os.environ["SCOUT_S3_ENDPOINT"]
bucket = os.environ["SCOUT_S3_BUCKET"]
s3 = boto3.client("s3", endpoint_url=endpoint)

last = None
for attempt in range(30):
    try:
        try:
            s3.head_bucket(Bucket=bucket)
            print(f"bucket {bucket} already exists")
        except ClientError:
            s3.create_bucket(Bucket=bucket)
            print(f"bucket {bucket} created")
        sys.exit(0)
    except (BotoCoreError, ClientError) as exc:
        last = exc
        time.sleep(2)
sys.exit(f"could not create bucket {bucket} at {endpoint}: {last}")
