# worker.py
# -*- coding: utf-8 -*-
import os
from rq import Worker, Queue, Connection
from redis import Redis

def main():
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    with Connection(Redis.from_url(redis_url)):
        Worker([Queue("conversions")]).work(with_scheduler=False)

if __name__ == "__main__":
    main()
