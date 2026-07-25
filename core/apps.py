from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "pve-helper core"

    def ready(self):
        import core.checks  # noqa: F401
        import core.signals  # noqa: F401

        _publish_certificates_on_first_connection()


def _publish_certificates_on_first_connection() -> None:
    """Write the certificate volume once, as soon as a database connection exists.

    The published CA bundle is what `REQUESTS_CA_BUNDLE` points at, so it has to be
    on disk before the first outbound call rather than after the first UI action:
    OpenSSL treats a missing bundle path as a verification failure, not as "fall back
    to the system store". Publishing here also lets nginx pick up a certificate that
    was selected while it was down.

    Hooked to the first connection rather than run directly in `ready()` because a
    query there runs before the app registry is populated — Django warns about it,
    and rightly so. Every process that talks to the database gets this, including the
    queue workers and the console, which is what makes the file's existence
    independent of anyone opening a page.
    """
    from django.db.backends.signals import connection_created
    from django.dispatch import receiver

    # `weak=False` is load-bearing: the receiver is a local function, so the default
    # weak reference is collected the moment this function returns and the signal
    # then fires into nothing — silently, because publication is best-effort.
    @receiver(connection_created, dispatch_uid="core.certificate_store.publish", weak=False)
    def _publish(sender, connection, **kwargs):
        connection_created.disconnect(dispatch_uid="core.certificate_store.publish")
        from core.services.certificate_store import publish_quietly

        # An absent volume and an unmigrated database are both normal at boot and
        # are swallowed; the next successful publication overwrites what this skipped.
        publish_quietly()
