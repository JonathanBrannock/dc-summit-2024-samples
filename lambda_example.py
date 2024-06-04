import json
import urllib3
import boto3
import uuid
from datetime import datetime
import time
from os import environ

con_pool = urllib3.PoolManager()

# Get environment variables from the Lambda configuration.
sqs_source_queue = environ.get("SQS_SOURCE_QUEUE")    # SQS queue to pull tasks from
sqs_prov_queue = environ.get("SQS_PROV_QUEUE")        # SQS queue to submit provanance events
componentId = environ.get("COMPONENT_ID")             # This LambdaFunctions componentID, which is a UUID. Usefull for filtering event data. 
componentType = environ.get("COMPONENT_TYPE")         # The component type, Like FetchHTTPLambda
componentName = environ.get("COMPONENT_NAME")         # The component name, Like MyHTTPLambdaProcessor 
processGroupName = environ.get("PROCESS_GROUP_NAME")  # The process group name, Usefull for grouping event data


def transfer(payload):
    # Setup S3 connection
    s3 = boto3.client('s3')

    # Get transfer specifics from message payload
    url = payload['url']
    bucket = payload['bucket']
    key = payload['key']
    overwrite = payload['overwrite']

    # Check if the object is already there and if so, skip it.
    if overwrite.lower() == "false":
        try:
            try_head = s3.head_object(Bucket=bucket, Key=key)
            if int(try_head['ContentLength']) == int(payload['size']):
                print("Object Exists and is the correct size. Skipping.")
                return None
        except Exception as e:
            pass  # Squash exceptions here because its normal.

    # Perform the transfer in memory
    fetch_start = time.time()
    with con_pool.request('GET', url, preload_content=False) as resp:
        fetch_duration = int((time.time() - fetch_start) * 1000)
        send_start = time.time()
        s3.upload_fileobj(resp, bucket, key)
    resp.release_conn()
    send_duration = int((time.time() - send_start) * 1000)

    # Check file size to verify
    dest_object_head = s3.head_object(Bucket=bucket, Key=key)
    if 'size' in payload:
        orgin_size = int(payload['size'])
        dest_size = int(dest_object_head['ContentLength'])
        assert orgin_size == dest_size

    # Report Provenance to NIFI
    prov_fetch_event = {
        "eventId": str(uuid.uuid4()),
        "eventType": "FETCH",
        "timestampMillis": int(time.time() * 1000),
        "timestamp": datetime.utcnow().isoformat(timespec='milliseconds') + "Z",
        "durationMillis": fetch_duration,
        "componentId": componentId,
        "componentType": componentType,
        "componentName": componentName,
        "processGroupName": processGroupName,
        "entityId": payload['uuid'],
        "entityType": "org.apache.nifi.flowfile.FlowFile",
        "entitySize": int(payload['size']),
        "previousAttributes": payload,
        "transitUri": url,
        "platform": "aws",
        "application": "Lambda Flow"
    }

    prov_send_event = {
        "eventId": str(uuid.uuid4()),
        "eventType": "SEND",
        "timestampMillis": int(time.time() * 1000),
        "timestamp": datetime.utcnow().isoformat(timespec='milliseconds') + "Z",
        "durationMillis": send_duration,
        "componentId": componentId,
        "componentType": componentType,
        "componentName": componentName,
        "processGroupName": processGroupName,
        "entityId": payload['uuid'],
        "entityType": "org.apache.nifi.flowfile.FlowFile",
        "entitySize": int(dest_object_head['ContentLength']),
        "previousEntitySize": payload['size'],
        "updatedAttributes": {
            "s3.key": key,
            "s3.etag": dest_object_head['ETag'].strip('"'),
            "s3.storeClass": "STANDARD",
            "s3.apimethod": "multipartupload",
            "s3.bucket": bucket
        },
        "previousAttributes": payload,
        "transitUri": url,
        "platform": "aws",
        "application": "Lambda Flow"
    }

    return [prov_fetch_event, prov_send_event]

# Default lambda_handler
def lambda_handler(event, context):
    sqs = boto3.client('sqs')  # Using Lambda Execution Role
    # There may be multiple Records for each event
    for record in event['Records']:
        try:
            # Try to load message body from the record (Type String) using JSON parser
            body = json.loads(record['body'])
        except TypeError:
            # Else, load the mapped Python Dict as the body
            body = record['body']
        try:
            # Run the transfer fuction and capture any provenance envents
            prov = transfer(body)
            if prov is not None:
                # If we received provinance data, send it to the provinance queue. 
                sqs.send_message(QueueUrl=sqs_prov_queue, MessageBody=json.dumps(prov))
        except Exception as e:
            # Print the exception, so that its logged in CloudWatch, then re-raise it. 
            print(e, record)
            raise e
        else:
            # If everything is successfull, delete the message from SQS. 
            if record['receiptHandle'] != "MessageReceiptHandle":
                sqs.delete_message(QueueUrl=sqs_source_queue, ReceiptHandle=record['receiptHandle'])
