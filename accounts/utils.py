import threading

# This is the isolated notepad for each server worker
_thread_locals = threading.local()

def set_current_user_context(pharmacy, is_superuser=False):
    _thread_locals.pharmacy = pharmacy
    _thread_locals.is_superuser = is_superuser

def get_current_pharmacy():
    return getattr(_thread_locals, 'pharmacy', None)

def is_current_user_superuser():
    return getattr(_thread_locals, 'is_superuser', False)