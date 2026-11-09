"""
Shared Limiter instance. Lives in its own module (not app/main.py) so
routers can import it without a circular import — main.py imports the
routers, so a router can't import the limiter back out of main.py.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings

# Without storage_uri, slowapi counts in-process memory — fine for one
# worker, wrong for more than one (see Settings.redis_url's docstring).
# storage_uri=None (the default when redis_url is unset) keeps the
# in-memory behavior exactly as before for every deployment that hasn't
# opted into Redis.
limiter = Limiter(key_func=get_remote_address, storage_uri=settings.redis_url or None)
