"""Weekly to-do board — Flask blueprint.

GET  /todos                      the page
GET  /api/todos?week=YYYY-MM-DD  a week's board (default: current)
POST /api/todos                  {text}          add to current week's Prioritize
PATCH /api/todos/<id>            {bucket?,text?} move/rename in current week
DELETE /api/todos/<id>                           remove from current week

All routes sit behind the app-wide auth gate. Mutations only ever touch the
current week; past weeks are read-only by construction (the store rejects ids
not in the current week).
"""

import os

from flask import Blueprint, jsonify, render_template, request

from config import DATA_DIR, ALERT_SCHEDULE_TZ
from security import client_ip, rate_limit_ok
from todo_store import TodoStore

todo_bp = Blueprint("todo", __name__)

# Week boundary follows the same timezone as the alert schedule, so "Monday"
# means the same thing across the app.
todos = TodoStore(os.path.join(DATA_DIR, "todos"), tz_name=ALERT_SCHEDULE_TZ)


@todo_bp.route("/todos")
def todos_page():
    return render_template("todos.html")


@todo_bp.route("/api/todos", methods=["GET"])
def get_todos():
    return jsonify(todos.board(request.args.get("week")))


@todo_bp.route("/api/todos", methods=["POST"])
def add_todo():
    if not rate_limit_ok(f"todo:{client_ip(request)}", 60, 60):
        return jsonify({"error": "Too many requests — slow down a moment."}), 429
    data = request.get_json(silent=True) or {}
    try:
        task = todos.add(data.get("text", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(task), 201


@todo_bp.route("/api/todos/<task_id>", methods=["PATCH"])
def update_todo(task_id):
    data = request.get_json(silent=True) or {}
    bucket = data.get("bucket")
    text = data.get("text")
    if bucket is None and text is None:
        return jsonify({"error": "nothing to update"}), 400
    if not todos.update(task_id, bucket=bucket, text=text):
        # Not found in the current week, or invalid bucket/text — past weeks
        # are read-only, so this is the expected "can't edit that" response.
        return jsonify({"error": "task not found in the current week"}), 404
    return jsonify({"id": task_id, "bucket": bucket, "text": text})


@todo_bp.route("/api/todos/<task_id>", methods=["DELETE"])
def delete_todo(task_id):
    if not todos.delete(task_id):
        return jsonify({"error": "task not found in the current week"}), 404
    return jsonify({"deleted": task_id})
