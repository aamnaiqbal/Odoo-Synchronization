import logging
from datetime import timedelta

import requests

from odoo import api, fields, models, _


_logger = logging.getLogger(__name__)


class SyncQueue(models.Model):
    _name = "sync.queue"
    _description = "Synchronization Queue"
    _order = "priority desc, id asc"

    name = fields.Char(
        required=True,
        readonly=True,
    )

    model_name = fields.Char(
        required=True,
        index=True,
        readonly=True,
    )

    record_id = fields.Integer(
        required=True,
        index=True,
        readonly=True,
    )

    sync_uuid = fields.Char(
        required=True,
        index=True,
        readonly=True,
    )

    operation = fields.Selection(
        [
            ("create", "Create"),
            ("update", "Update"),
            ("delete", "Delete"),
        ],
        required=True,
        readonly=True,
    )

    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("processing", "Processing"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="pending",
        required=True,
        index=True,
    )

    priority = fields.Integer(
        default=10,
        index=True,
    )

    retry_count = fields.Integer(
        default=0,
    )

    max_retries = fields.Integer(
        default=10,
    )

    next_retry_at = fields.Datetime(
        default=fields.Datetime.now,
        index=True,
    )

    last_error = fields.Text(
        readonly=True,
    )

    started_at = fields.Datetime(
        readonly=True,
    )

    completed_at = fields.Datetime(
        readonly=True,
    )

    connection_id = fields.Many2one(
        "sync.connection",
        required=True,
        readonly=True,
        ondelete="restrict",
    )

    remote_id = fields.Integer(
        readonly=True,
    )

    @api.model
    def enqueue(
        self,
        model_name,
        record_id,
        sync_uuid,
        operation,
        priority=10,
    ):
        connection = self.env["sync.connection"].sudo().search(
            [("active", "=", True)],
            limit=1,
        )

        if not connection:
            return False

        # Avoid duplicate pending jobs for the same record.
        existing = self.search([
            ("model_name", "=", model_name),
            ("record_id", "=", record_id),
            ("sync_uuid", "=", sync_uuid),
            ("state", "in", ["pending", "processing"]),
        ], limit=1)

        if existing:
            if existing.state == "pending" and existing.priority < priority:
                existing.priority = priority
            if operation == "create" and existing.operation == "update":
                existing.write({
                    "operation": "create",
                    "name": f"{model_name}:{record_id}:create",
                })

            return existing

        return self.create({
            "name": f"{model_name}:{record_id}:{operation}",
            "model_name": model_name,
            "record_id": record_id,
            "sync_uuid": sync_uuid,
            "operation": operation,
            "connection_id": connection.id,
            "priority": priority,
        })

    @api.model
    def _cron_process_queue(self):
        connection = self.env["sync.connection"].sudo().search(
            [("active", "=", True)],
            limit=1,
        )

        if not connection:
            return

        # The baseline tells the records of the imported database apart from the new
        # ones; relations cannot be resolved without it.
        if not connection.baseline_date:
            _logger.info(
                "Sync queue paused: capture the baseline on connection %s first.",
                connection.name,
            )
            return

        jobs = self._get_jobs(connection.batch_size or 50)

        for index, job in enumerate(jobs):
            try:
                job._process_job()
            except requests.RequestException as error:
                # Remote unreachable (internet outage, server restarting): expected
                # for an offline-first setup. The job stays pending without using up
                # a retry, the jobs not tried yet are handed back and the batch stops.
                _logger.warning("Remote server unavailable: %s", error)
                job._defer(str(error))
                jobs[index + 1:]._release()
                self.env.cr.commit()
                break
            except Exception as error:
                _logger.exception("Synchronization failed for queue %s", job.id)
                job._schedule_retry(str(error))

            self.env.cr.commit()

    @api.model
    def _get_jobs(self, limit):
        now = fields.Datetime.now()

        jobs = self.search([
            ("state", "=", "pending"),
            ("next_retry_at", "<=", now),
        ], order="priority desc, id asc", limit=limit)

        result = self.env[self._name]

        for job in jobs:
            try:
                self.env.cr.execute(
                    """
                    SELECT id
                    FROM sync_queue
                    WHERE id = %s
                    AND state = 'pending'
                    FOR UPDATE SKIP LOCKED
                    """,
                    (job.id,),
                )

                row = self.env.cr.fetchone()

                if not row:
                    continue

                job.write({
                    "state": "processing",
                    "started_at": fields.Datetime.now(),
                })

                result |= job

            except Exception:
                continue

        return result

    def _process_job(self):
        self.ensure_one()

        if self.operation == "delete":
            payload = self._prepare_delete_payload()
        else:
            payload = self._prepare_record_payload()

        _logger.debug("[SYNC] queue %s payload: %s", self.id, payload)

        result = self.connection_id._json2_call(
            "sync.registry",
            "apply",
            payload,
        )

        _logger.debug("[SYNC] queue %s remote response: %s", self.id, result)

        remote_id = result.get("remote_id")
        warnings = result.get("warnings") or []
        self._store_remote_id(result.get("operation"), remote_id)

        self.write({
            "state": "done",
            "remote_id": remote_id,
            "completed_at": fields.Datetime.now(),
            "last_error": ("Warnings:\n" + "\n".join(warnings)) if warnings else False,
        })

    def _store_remote_id(self, operation, remote_id):
        """Keep local id <-> remote id, so later pushes can resolve the relations that
        point to this record (see sync.record.map)."""
        self.ensure_one()
        Map = self.env["sync.record.map"].sudo()
        if operation == "delete":
            Map.search([
                ("connection_id", "=", self.connection_id.id),
                ("model", "=", self.model_name),
                ("local_id", "=", self.record_id),
            ]).unlink()
        elif remote_id and self.env[self.model_name].browse(self.record_id).exists():
            Map._set(self.connection_id, self.model_name, self.record_id, remote_id)

    def _prepare_delete_payload(self):
        remote_id = self.env["sync.record.map"].sudo()._get_remote_id(
            self.connection_id, self.model_name, self.record_id
        )
        return {
            "model_name": self.model_name,
            "sync_uuid": self.sync_uuid,
            "operation": "delete",
            "remote_id": remote_id or False,
        }

    def _prepare_record_payload(self):
        record = self.env[self.model_name].browse(self.record_id)

        if not record.exists():
            return self._prepare_delete_payload()

        values = record._sync_prepare_values(self.connection_id)
        remote_id = self.env["sync.record.map"].sudo()._get_remote_id(
            self.connection_id, self.model_name, self.record_id
        )

        return {
            "model_name": self.model_name,
            "sync_uuid": self.sync_uuid,
            "operation": self.operation,
            "values": values,
            "remote_id": remote_id or False,
        }

    def _schedule_retry(self, error):
        self.ensure_one()

        retry_count = self.retry_count + 1

        if retry_count >= self.max_retries:
            self.write({
                "state": "failed",
                "retry_count": retry_count,
                "last_error": error[:5000],
            })
            return

        delay_minutes = min(
            60,
            2 ** min(retry_count, 6),
        )

        next_retry = fields.Datetime.now() + timedelta(
            minutes=delay_minutes
        )

        self.write({
            "state": "pending",
            "retry_count": retry_count,
            "next_retry_at": next_retry,
            "last_error": error[:5000],
        })

    def _defer(self, error, delay_minutes=1):
        """Back to pending WITHOUT counting a failed attempt (remote unreachable)."""
        self.write({
            "state": "pending",
            "next_retry_at": fields.Datetime.now() + timedelta(minutes=delay_minutes),
            "last_error": error[:5000],
        })

    def _release(self):
        """Give jobs back to the queue untouched (picked up by the worker, not tried)."""
        self.write({
            "state": "pending",
            "started_at": False,
        })

    def action_retry(self):
        for job in self:
            job.write({
                "state": "pending",
                "retry_count": 0,
                "next_retry_at": fields.Datetime.now(),
                "last_error": False,
            })

    def action_reset_processing(self):
        for job in self.filtered(
            lambda job: job.state == "processing"
        ):
            job.write({
                "state": "pending",
                "next_retry_at": fields.Datetime.now(),
            })
