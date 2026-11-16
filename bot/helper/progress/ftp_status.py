from .s3_status import S3UploadStatus


class FTPUploadStatus(S3UploadStatus):
    """An upload to an FTP server: the bucket line, under its own tool name.

    ``BaseStatus`` is all that is needed, and the bucket status already is that
    plus the upload direction; the only thing that differs is which tool
    ``/status`` groups the task under and asks for a speed.
    """

    tool = "ftp"
