import os
import json
import logging
import requests
import boto3
import uuid
from datetime import datetime
import time

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def transfer(body: dict) -> list:
    """
    Given full routing informatin for file name extracted from the nifi message,
    send it to the appropriate destination.

        Parameters:
                body (dict): Dictionary parsed from JSON from NIFI SQS Message
        Returns:
                nifi_provenance (list): List of the provenance from the fetch event and the send event

    """
    logger.info(f"Preparing to load {file_name} and put in {bucket}.")  
    # Setup S3 connection
    s3 = boto3.client("s3")

    # Get information from the message data
    file_name = body["filename"]
    bucket = body["Bucket"]
    url = body["source_url"]
    key = body["Key"]
    overwrite = body["overwrite"]

    # Check if the file is already there and if so, skip it.
    if overwrite.lower() == "false":
        try:
            try_head = s3.head_object(Bucket=bucket, Key=key)
            if int(try_head["ContentLength"]) == int(body["file.size"]):
                print("Object Exists and is the correct size. Skipping.")
                return None
        except Exception as e:
            pass  # Squash exceptions here because its normal.

    # Perform the transfer in memory
    fetch_start = time.time()
    response = requests.get(url, stream=True, timeout=120)
    content = response.content
    fetch_duration = int((time.time() - fetch_start) * 1000)

    # Verify that the data we downloaded is the correct size.
    if "file.size" in body:
        origin_size = int(body["file.size"])
        content_size = int(response.headers["Content-Length"])

        if origin_size != content_size:
            logger.info("File Sizes Do Not Match")
            logger.info(f"Origin Size: {origin_size}")
            logger.info(f"Content Size: {content_size}")

        assert content_size == origin_size

    send_start = time.time()
    s3.put_object(Bucket=bucket, Key=key, Body=content, ACL="public-read")
    send_duration = int((time.time() - send_start) * 1000)
    del content

    logger.info(f"Fetched data in {fetch_duration}.")
    logger.info(f"Sent data in {send_duration}.")

    # Check file size to verify
    dest_object_head = s3.head_object(Bucket=bucket, Key=key)
    if "file.size" in body:
        origin_size = int(body["file.size"])
        dest_size = int(dest_object_head["ContentLength"])
        logger.info(origin_size)
        logger.info(dest_size)
        assert origin_size == dest_size

    componentId = body["COMPONENT_ID"]
    componentName = body["COMPONENT_NAME"]
    processGroupName = body["PROCESS_GROUP_NAME"]
    componentType = body["COMPONENT_TYPE"]

    # Report NiFi like provenance events
    prov_fetch_event = {
        "eventId": str(uuid.uuid4()),
        "eventType": "FETCH",
        "timestampMillis": int(time.time() * 1000),
        "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "durationMillis": fetch_duration,
        "componentId": componentId,
        "componentType": componentType,
        "componentName": componentName,
        "processGroupName": processGroupName,
        "entityId": body["uuid"],
        "entityType": "org.apache.nifi.flowfile.FlowFile",
        "entitySize": int(body["file.size"]),
        "previousAttributes": body,
        "transitUri": url,
        "platform": "aws",
        "application": "Lambda Flow",
    }

    prov_send_event = {
        "eventId": str(uuid.uuid4()),
        "eventType": "SEND",
        "timestampMillis": int(time.time() * 1000),
        "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "durationMillis": send_duration,
        "componentId": componentId,
        "componentType": componentType,
        "componentName": componentName,
        "processGroupName": processGroupName,
        "entityId": body["uuid"],
        "entityType": "org.apache.nifi.flowfile.FlowFile",
        "entitySize": int(dest_object_head["ContentLength"]),
        "previousEntitySize": body["file.size"],
        "updatedAttributes": {
            "s3.key": key,
            "s3.etag": dest_object_head["ETag"].strip('"'),
            "s3.storeClass": "STANDARD",
            "s3.apimethod": "multipartupload",
            "s3.bucket": bucket,
        },
        "previousAttributes": body,
        "transitUri": url,
        "platform": "aws",
        "application": "Lambda Flow",
    }

    nifi_provenance = [prov_fetch_event, prov_send_event]

    return nifi_provenance


def lambda_handler(event, context):
    sqs = boto3.client("sqs")
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
        except TypeError:
            body = record["body"]

        logger.info(body)

        try:
            prov = transfer(body=body)
            if prov is not None:
                sqs.send_message(
                    QueueUrl=body["SQS_PROV_QUEUE"], MessageBody=json.dumps(prov)
                )
        except Exception as e:
            logger.info(e, record)
            raise e

    return {"statusCode": 200, "body": json.dumps("Successfully transferred data.")}
