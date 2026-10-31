import os
import socket
import time

import pymysql
from flask import Flask, jsonify, request

app = Flask(__name__)
HOSTNAME = socket.gethostname()   # the Pod name when running in Kubernetes


def get_conn():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],   # from the Secret
        database=os.environ["DB_NAME"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def init_db(retries=30):
    for _ in range(retries):
        try:
            with get_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS entries (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(100) NOT NULL,
                        message VARCHAR(500) NOT NULL,
                        served_by VARCHAR(100) NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )""")
            return
        except pymysql.err.OperationalError as e:
            print("DB not ready, retrying...", e, flush=True)
            time.sleep(2)
    raise SystemExit("could not reach the database")


init_db()


@app.post("/entries")
def add_entry():
    data = request.get_json(silent=True) or {}
    name, message = data.get("name"), data.get("message")
    if not name or not message:
        return jsonify(error="name and message required"), 400
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO entries (name, message, served_by) VALUES (%s, %s, %s)",
            (name, message, HOSTNAME),
        )
        new_id = cur.lastrowid
    return jsonify(id=new_id, pod=HOSTNAME), 201


@app.get("/entries")
def list_entries():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM entries ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
    return jsonify(pod=HOSTNAME, entries=rows)


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)