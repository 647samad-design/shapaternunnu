from django.apps import AppConfig


class BeesConfig(AppConfig):
    name = 'bees'
    verbose_name = 'Store'

    def ready(self):
        from django.db.models.signals import post_save
        from . import webpush
        from .models import Notification
        post_save.connect(webpush.on_notification, sender=Notification, dispatch_uid="webpush-notification")
        from . import ops
        ops.connect_signals()
