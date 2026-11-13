"""Create the S3 bucket the server writes to. Safe to run repeatedly.

Runs once inside the atlas-mcp image (it already has boto3), so no extra image
has to be pulled and pinned.
"""

import os

import boto3
from botocore.exceptions import ClientError

endpoint = os.environ["ATLAS_S3_ENDPOINT"]
bucket = os.environ["ATLAS_S3_BUCKET"]
s3 = boto3.client("s3", endpoint_url=endpoint)

try:
    s3.head_bucket(Bucket=bucket)
    print(f"bucket {bucket} already exists")
except ClientError:
    s3.create_bucket(Bucket=bucket)
    print(f"bucket {bucket} created")
