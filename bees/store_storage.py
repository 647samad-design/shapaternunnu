"""Storage backends that keep each store's uploads under stores/<slug>/
(used only in multi-store mode, see bees.multistore)."""
from django.core.files.storage import FileSystemStorage

from .multistore import StoreFilesMixin


class StoreFileSystemStorage(StoreFilesMixin, FileSystemStorage):
    pass


try:
    from storages.backends.s3 import S3Storage
except ImportError:  # django-storages not installed
    S3Storage = None

if S3Storage is not None:
    class StoreS3Storage(StoreFilesMixin, S3Storage):
        pass
