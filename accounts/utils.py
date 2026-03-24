import threading

_thread_locals = threading.local()


def set_current_user_context(pharmacy, is_superuser=False, organization=None):
    _thread_locals.pharmacy = pharmacy
    _thread_locals.is_superuser = is_superuser
    _thread_locals.organization = organization


def get_current_pharmacy():
    return getattr(_thread_locals, 'pharmacy', None)


def get_current_organization():
    return getattr(_thread_locals, 'organization', None)


def is_current_user_superuser():
    return getattr(_thread_locals, 'is_superuser', False)