"""`python -m app.mailin`: receive-only mail server (its own Docker container, port 25 → 2525)."""
from __future__ import annotations

import logging
import signal
import threading

from app.core.config import settings
from app.core.db import init_db
from app.mailin.server import make_controller, tls_context


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("mailin")
    if not settings.inbound_domain:
        log.warning("INBOUND_DOMAIN is not set: mail receiving is off")
    init_db()
    tls = tls_context(settings.inbound_domain or "localhost", settings.data_dir / "mailin-tls")
    controller = make_controller(tls=tls)
    controller.start()
    log.info("receiving mail for *@%s on port %s", settings.inbound_domain, settings.mailin_port)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    controller.stop()


if __name__ == "__main__":
    main()
