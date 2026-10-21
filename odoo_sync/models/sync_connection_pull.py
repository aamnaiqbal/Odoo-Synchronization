import logging
import time
from datetime import datetime

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .sync_pull_cursor import PULL_DT_FORMAT

_logger = logging.getLogger(__name__)

# Dependencies are pulled on demand (a sale line needs its order, a product needs its
# template ...). This only stops a runaway chain.
PULL_MAX_DEPTH = 25

# Context applied to everything the pull writes:
#   skip_sync        the written records are never queued back to the remote (no loop)
#   tracking/mail    no chatter messages or e-mails caused by synchronized data
PULL_CONTEXT = {
    "skip_sync": True,
    "active_test": False,
    "tracking_disable": True,
    "mail_notrack": True,
    "mail_create_nolog": True,
}


class SyncConnectionPull(models.Model):
    """LOCAL side of the pull (remote -> local).

    For every model flagged `_sync_pull = True` the remote is asked for the records
    written after the cursor (sync.pull.cursor); each one is created or updated here.

    Which local record is it?   remote id -> sync.record.map / same id if it came
                                with the import / sync_uuid / xmlid / model hook.
    Relations                   keys sent by the remote are resolved to local ids; a
                                dependency that does not exist here yet is pulled first.
    Conflicts                   a local record with an unsent change (pending, processing
                                or failed job in sync.queue) is NOT overwritten: the
                                local change wins and is pushed.
    """

    _inherit = "sync.connection"

    pull_enabled = fields.Boolean(
        string="Pull From Remote",
        default=False,
        help="The cron fetches the records changed on the remote into this database.",
    )
    last_pull_date = fields.Datetime(readonly=True, copy=False)

    # ------------------------------------------------------------------
    # baseline
    # ------------------------------------------------------------------
    def _capture_baseline(self):
        """The starting cursor of every pulled model is taken together with the baseline,
        while the database still holds only imported records: the newest write_date of a
        model is then exactly where the remote export stopped."""
        count = super()._capture_baseline()
        Cursor = self.env["sync.pull.cursor"].sudo()
        Cursor.search([("connection_id", "=", self.id)]).unlink()
        for Model in self._pull_get_models():
            Cursor._get_or_create(self, Model._name)
        return count

    # ------------------------------------------------------------------
    # entry points
    # ------------------------------------------------------------------
    @api.model
    def _cron_pull(self):
        for connection in self.search([("pull_enabled", "=", True)]):
            if not connection.baseline_date:
                continue
            try:
                connection._pull(commit=True)
            except (requests.RequestException, UserError) as error:
                # Remote unreachable or refused: nothing is lost, the next run retries.
                self.env.cr.rollback()
                self.env.invalidate_all()
                _logger.warning("[SYNC PULL] %s: %s", connection.name, error)

    def action_pull_now(self):
        self.ensure_one()
        if not self.baseline_date:
            raise UserError(_("Capture the baseline first."))
        try:
            summary = self._pull(commit=False, time_budget=25)
        except requests.RequestException as error:
            raise UserError(_("Connection failed:\n%s", error)) from error
        lines, failed = [], False
        for model_name, stats in summary.items():
            line = _(
                "%(model)s: %(created)s created, %(updated)s updated, %(skipped)s kept local",
                model=model_name, created=stats["created"], updated=stats["updated"],
                skipped=stats["skipped"],
            )
            if stats["error"]:
                failed = True
                line += "\n  ERROR " + stats["error"]
            lines.append(line)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Pull finished"),
                "message": "\n".join(lines) or _("No model is flagged for pull."),
                "type": "warning" if failed else "success",
                "sticky": failed,
            },
        }

    def _pull_get_models(self):
        """Models flagged `_sync_pull`, in `_sync_pull_sequence` order."""
        models_ = []
        for name in self.env.registry:
            Model = self.env[name]
            if getattr(Model, "_sync_pull", False) and getattr(Model, "_sync_enabled", False):
                models_.append(Model)
        return sorted(models_, key=lambda m: (m._sync_pull_sequence, m._name))

    def _pull(self, commit=False, time_budget=45):
        """Pull every flagged model. `commit=True` (cron) commits after every batch."""
        self.ensure_one()
        deadline = time.monotonic() + time_budget
        summary = {}
        for Model in self._pull_get_models():
            if time.monotonic() >= deadline:
                break
            summary[Model._name] = self._pull_model(Model, deadline, commit)
        self.last_pull_date = fields.Datetime.now()
        if commit:
            self.env.cr.commit()
        return summary

    # ------------------------------------------------------------------
    # one model
    # ------------------------------------------------------------------
    def _pull_model(self, Model, deadline, commit):
        cursor = self.env["sync.pull.cursor"].sudo()._get_or_create(self, Model._name)
        stats = {"created": 0, "updated": 0, "skipped": 0, "error": False}
        state = {"visiting": set(), "warnings": []}
        position = None  # (write_date, id) of the last record applied
        saved = {"created": 0, "updated": 0, "skipped": 0}  # already added to the cursor totals

        while time.monotonic() < deadline:
            result = self._json2_call(
                "sync.registry",
                "pull_changes",
                {
                    "model_name": Model._name,
                    "write_date": (position or (cursor.last_write_date, cursor.last_id))[0],
                    "last_id": (position or (cursor.last_write_date, cursor.last_id))[1],
                    "limit": self.batch_size,
                },
            )
            failed = False
            for data in result.get("records", []):
                try:
                    # One savepoint per record: a failure undoes only that record
                    # (and the dependencies pulled for it).
                    with self.env.cr.savepoint():
                        outcome = self._pull_apply(Model, data, state)
                except requests.RequestException:
                    raise
                except Exception as error:  # noqa: BLE001 - reported on the cursor
                    _logger.exception("[SYNC PULL] %s #%s failed", Model._name, data.get("id"))
                    stats["error"] = f"{Model._name} #{data.get('id')}: {error}"
                    state["visiting"].clear()
                    failed = True
                    break
                stats[outcome] += 1
                position = (data["write_date"], data["id"])
            if failed:
                break  # the cursor stays before the failing record: it is retried
            if result.get("cursor"):
                last = result["cursor"]
                position = (last["write_date"], last["id"])
            if commit and position:
                self._pull_save_cursor(cursor, position, stats, saved)
                self.env.cr.commit()
            if not result.get("has_more"):
                break

        self._pull_save_cursor(cursor, position, stats, saved)
        for warning in state["warnings"][:20]:
            _logger.info("[SYNC PULL] %s", warning)
        if commit:
            self.env.cr.commit()
        return stats

    def _pull_save_cursor(self, cursor, position, stats, saved):
        """Store the position and add to the totals what was not stored yet."""
        vals = {
            "last_run": fields.Datetime.now(),
            "last_error": stats["error"] or False,
        }
        for key in ("created", "updated", "skipped"):
            vals[key + "_count"] = getattr(cursor, key + "_count") + stats[key] - saved[key]
            saved[key] = stats[key]
        if position:
            vals["last_write_date"], vals["last_id"] = position
        cursor.write(vals)

    # ------------------------------------------------------------------
    # one record
    # ------------------------------------------------------------------
    def _pull_apply(self, Model, data, state):
        """Create or update the local record for one remote record.
        Returns 'created', 'updated' or 'skipped'."""
        Model = Model.with_context(**PULL_CONTEXT)
        remote_id = data["id"]
        mark = (Model._name, remote_id)
        if len(state["visiting"]) > PULL_MAX_DEPTH:
            raise UserError(_("Dependency chain too deep at %s #%s.", *mark))

        local = self._pull_find_local(Model, data, state)
        if local and self._pull_has_unsent_change(Model._name, local.id):
            return "skipped"

        state["visiting"].add(mark)
        try:
            values = self._pull_resolve_values(Model, data["values"], state)
            if local:
                if values:
                    local.write(values)
                outcome = "updated"
            else:
                if not Model._sync_pull_allow_create:
                    raise UserError(
                        _("%s #%s has no local counterpart and cannot be created here.", *mark)
                    )
                keys = (data.get("key") or {}).get("sync_keys") or []
                if len(keys) == 1:
                    # Same uuid on both sides: a later push of this record updates the
                    # remote registry row instead of adding a second one.
                    values["sync_uuid"] = keys[0]
                local = Model.create(values)
                outcome = "created"
            self._pull_store_map(Model, local, remote_id, data.get("write_date"))
        finally:
            state["visiting"].discard(mark)
        return outcome

    def _pull_find_local(self, Model, data, state):
        """The local record this remote record corresponds to (empty if none)."""
        local_id = self._pull_lookup_key(data.get("key") or {"model": Model._name, "remote_id": data["id"]})
        if local_id:
            return Model.browse(local_id)
        return Model._sync_pull_locate(self, data, state)

    def _pull_has_unsent_change(self, model_name, local_id):
        return bool(
            self.env["sync.queue"].sudo().search_count(
                [
                    ("model_name", "=", model_name),
                    ("record_id", "=", local_id),
                    ("state", "in", ("pending", "processing", "failed")),
                ],
                limit=1,
            )
        )

    def _pull_store_map(self, Model, local, remote_id, write_date=None):
        """Remember local <-> remote for records that do not share their id. Records that
        came with the import (same id on both sides) need no row."""
        if remote_id <= self._get_baseline_max_id(Model._name) and local.id == remote_id:
            return
        remote_dt = False
        if write_date:
            remote_dt = datetime.strptime(write_date, PULL_DT_FORMAT).replace(microsecond=0)
        self.env["sync.record.map"].sudo()._set(self, Model._name, local.id, remote_id, remote_dt)

    # ------------------------------------------------------------------
    # relations
    # ------------------------------------------------------------------
    def _pull_lookup_key(self, key):
        """Local id for a key sent by the remote, without contacting it (False if none).
        Tried in this order: remote_id (map / imported), xmlid, sync_uuid."""
        model = key.get("model")
        if not model or model not in self.env:
            return False
        Model = self.env[model].with_context(active_test=False)
        remote_id = int(key["remote_id"]) if key.get("remote_id") else False

        if remote_id:
            local_id = self.env["sync.record.map"].sudo()._get_local_id(self, model, remote_id)
            if local_id:
                return local_id

        found = Model.browse()
        if key.get("xmlid"):
            record = self.env.ref(key["xmlid"], raise_if_not_found=False)
            if record and record._name == model:
                found = record
        if not found and key.get("sync_keys") and "sync_uuid" in Model._fields:
            found = Model.search([("sync_uuid", "in", key["sync_keys"])], limit=1)
        if found:
            if remote_id:
                self._pull_store_map(Model, found, remote_id)
            return found.id
        return False

    def _pull_resolve_key(self, key, state, label=""):
        """Local id for a key; a dependency that is not here yet is pulled first."""
        local_id = self._pull_lookup_key(key)
        if local_id:
            return local_id

        model, remote_id = key.get("model"), key.get("remote_id")
        if model in self.env and remote_id and getattr(self.env[model], "_sync_enabled", False):
            mark = (model, remote_id)
            if mark in state["visiting"]:
                state["warnings"].append(f"{label}: circular reference to {model} #{remote_id}, skipped.")
                return False
            rows = self._json2_call(
                "sync.registry", "pull_records", {"model_name": model, "ids": [remote_id]}
            )
            if rows:
                self._pull_apply(self.env[model], rows[0], state)
                return self._pull_lookup_key(key)

        state["warnings"].append(f"{label}: {model} #{remote_id} cannot be found locally, skipped.")
        return False

    def _pull_resolve_values(self, Model, values, state):
        result = {}
        for name, value in values.items():
            field = Model._fields.get(name)
            if field is None or not Model._sync_is_syncable_field(name):
                continue  # field unknown here (different module version) or not synchronized
            label = f"{Model._name}.{name}"
            if field.type == "many2one":
                if not value:
                    result[name] = False
                    continue
                local_id = self._pull_resolve_key(value, state, label)
                if local_id:
                    result[name] = local_id
            elif field.type == "many2many":
                ids = [self._pull_resolve_key(key, state, label) for key in value or []]
                if all(ids):
                    result[name] = [(6, 0, ids)]
                else:
                    # Writing a partial list would unlink the records not found.
                    state["warnings"].append(f"{label}: some related records not found, field skipped.")
            else:
                result[name] = value
        return result
