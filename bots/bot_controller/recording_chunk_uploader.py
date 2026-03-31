import logging
import threading
from queue import Queue

import boto3
from django.conf import settings

logger = logging.getLogger(__name__)


class RecordingChunkUploader:
    def __init__(self, *, chunk_prefix: str, chunk_ext: str, chunk_mime_type: str, worker_count: int = 2, max_queue_size: int = 8):
        self.chunk_prefix = chunk_prefix.rstrip("/")
        self.chunk_ext = chunk_ext.lstrip(".")
        self.chunk_mime_type = chunk_mime_type
        self.queue = Queue(maxsize=max_queue_size)
        self.upload_errors = []
        self.uploaded_chunk_paths = []
        self._lock = threading.Lock()
        self._next_chunk_index = 0
        self._workers = []

        if settings.STORAGE_PROTOCOL == "azure":
            from azure.storage.blob import BlobServiceClient, ContentSettings

            self._azure_content_settings_class = ContentSettings
            options = settings.RECORDING_STORAGE_BACKEND.get("OPTIONS", {})
            if options.get("connection_string"):
                service_client = BlobServiceClient.from_connection_string(options["connection_string"])
            else:
                account_url = f"https://{options.get('account_name')}.blob.core.windows.net"
                service_client = BlobServiceClient(account_url=account_url, credential=options.get("account_key"))
            self._azure_container = options.get("azure_container")
            self._azure_service_client = service_client
            self._s3_client = None
            self._s3_bucket = None
        else:
            options = settings.RECORDING_STORAGE_BACKEND.get("OPTIONS", {})
            self._s3_client = boto3.client(
                "s3",
                endpoint_url=options.get("endpoint_url"),
                aws_access_key_id=options.get("access_key"),
                aws_secret_access_key=options.get("secret_key"),
            )
            self._s3_bucket = settings.AWS_RECORDING_STORAGE_BUCKET_NAME
            self._azure_service_client = None
            self._azure_container = None
            self._azure_content_settings_class = None

        for _ in range(max(1, worker_count)):
            worker = threading.Thread(target=self._upload_worker, daemon=True)
            worker.start()
            self._workers.append(worker)

    def _next_chunk_path(self):
        with self._lock:
            chunk_index = self._next_chunk_index
            self._next_chunk_index += 1
        return f"{self.chunk_prefix}/chunk_{chunk_index:04d}.{self.chunk_ext}"

    def enqueue_chunk(self, data: bytes):
        chunk_path = self._next_chunk_path()
        self.queue.put((chunk_path, data))
        return chunk_path

    def _upload_chunk(self, chunk_path: str, data: bytes):
        if self._azure_service_client is not None:
            blob_client = self._azure_service_client.get_blob_client(container=self._azure_container, blob=chunk_path)
            blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=self._azure_content_settings_class(content_type=self.chunk_mime_type),
            )
            return

        self._s3_client.put_object(
            Bucket=self._s3_bucket,
            Key=chunk_path,
            Body=data,
            ContentType=self.chunk_mime_type,
        )

    def _upload_worker(self):
        while True:
            item = self.queue.get()
            try:
                if item is None:
                    return

                chunk_path, data = item
                self._upload_chunk(chunk_path, data)
                with self._lock:
                    self.uploaded_chunk_paths.append(chunk_path)
            except Exception as exc:
                logger.exception("Recording chunk upload failed: %s", exc)
                with self._lock:
                    self.upload_errors.append(exc)
            finally:
                self.queue.task_done()

    def wait_for_uploads(self):
        self.queue.join()
        if self.upload_errors:
            raise RuntimeError(str(self.upload_errors[0]))
        return list(self.uploaded_chunk_paths)

    def shutdown(self):
        self.wait_for_uploads()
        for _ in self._workers:
            self.queue.put(None)
        for worker in self._workers:
            worker.join(timeout=5)
