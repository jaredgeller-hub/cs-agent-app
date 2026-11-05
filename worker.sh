#!/bin/bash
exec python3 -c "
import sys
print('[arq-worker] Starting worker process...', flush=True)

import asyncio
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
print('[arq-worker] Event loop created (Python 3.14 compat)', flush=True)

from product.worker import WorkerSettings
print('[arq-worker] WorkerSettings loaded', flush=True)

from arq.worker import Worker
worker = Worker(**{
    'functions': WorkerSettings.functions,
    'on_startup': WorkerSettings.on_startup,
    'redis_settings': WorkerSettings.redis_settings,
    'max_jobs': WorkerSettings.max_jobs,
    'job_timeout': WorkerSettings.job_timeout,
    'max_tries': WorkerSettings.max_tries,
})
print('[arq-worker] Worker instance created, starting main()...', flush=True)
loop.run_until_complete(worker.main())
"
