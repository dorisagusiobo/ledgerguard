"""In-memory S3 conditional writes and ordered requests; no AWS connections."""
from io import BytesIO

from botocore.exceptions import ClientError


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.bodies = []

    def get_object(self, *, Bucket, Key):
        self.calls.append(('get', Key))
        data = self.objects[Key]
        body = BytesIO(data)
        self.bodies.append(body)
        return {'Body': body, 'ContentLength': len(data)}

    def put_object(self, **request):
        key = request['Key']
        self.calls.append(('put', key))
        assert request['IfNoneMatch'] == '*'
        assert request['ServerSideEncryption'] == 'AES256'
        if key in self.objects:
            raise ClientError({'Error': {'Code': 'PreconditionFailed'},
                               'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
        self.objects[key] = request['Body']
        return {}


def event(bucket, batch_id):
    return {'Records': [{'eventSource': 'aws:s3', 's3': {
        'bucket': {'name': bucket}, 'object': {'key': f'batches/{batch_id}/manifest.json'}}}]}
