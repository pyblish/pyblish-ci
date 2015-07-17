import os
import sys
import threading

import ci
import app


if __name__ == '__main__':
    import argparse

    if "GITHUB_API_TOKEN" not in os.environ:
        app.log.info("GITHUB_API_TOKEN not set")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=8000, type=int)

    args = parser.parse_args()

    app.log.info("Starting worker..")
    t = threading.Thread(target=ci.worker)
    t.daemon = True
    t.start()

    app.log.info("Starting cleaner..")
    t = threading.Thread(target=ci.cleaner)
    t.daemon = True
    t.start()

    app.log.info("Starting front-end..")
    app.app.debug = True
    app.app.run(host='0.0.0.0', port=args.port)
